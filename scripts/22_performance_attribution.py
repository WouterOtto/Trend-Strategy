#!/usr/bin/env python3
"""
Script 22: Performance Attribution
====================================
Decompose portfolio returns by source: asset class, sector, position-level,
and alpha/beta decomposition vs benchmark.

Purpose:
    This script is the primary accounting tool for understanding WHAT drove
    portfolio performance over any given period.  It answers four questions:

        1. Which ASSET CLASSES contributed most? (stocks vs ETFs vs crypto)
        2. Which SECTORS contributed most? (Technology, Healthcare, etc.)
        3. Which POSITIONS drove returns? (top contributors & detractors)
        4. How much is SKILL vs MARKET? (alpha/beta decomposition)

    The analysis uses the Brinson-Hood-Beebower (BHB) attribution framework
    adapted for a long-only trend-following strategy:

        Contribution[i] = Weight[i] × Return[i]
        Active_Return   = Portfolio_Return − Benchmark_Return
        Information_Ratio = Active_Return / Tracking_Error

    All monetary values are expressed in EUR (base currency of the strategy).

Attribution Methodology:
    ─────────────────────────────────────────────────────────────────
    STEP 1  Collect realized P&L from trade_ledger.jsonl (closed trades)
    STEP 2  Collect unrealized P&L from portfolio_state.json (open positions)
    STEP 3  Mark open positions to current price via consolidated parquet
    STEP 4  Classify each position: asset class + sector (fundamentals)
    STEP 5  Compute position-level contribution = P&L / Starting_Portfolio_Value
    STEP 6  Aggregate contributions by asset class and sector
    STEP 7  Reconstruct daily portfolio returns from constituent price history
    STEP 8  Load benchmark daily returns (SPY.US or ACWI.US)
    STEP 9  Compute Beta = Cov(Rp, Rb) / Var(Rb)
    STEP 10 Compute Alpha = Annualized(mean(Rp) − Beta × mean(Rb))
    STEP 11 Compute Tracking Error = StdDev(Rp − Rb) × sqrt(252)
    STEP 12 Compute Information Ratio = Active_Return / Tracking_Error
    STEP 13 Export JSON attribution file + PDF report
    ─────────────────────────────────────────────────────────────────

Inputs:
    data/trade_ledger.jsonl                             (Script 13 output)
    data/portfolio_state.json                           (Script 13 output)
    data_cache/fundamentals/company_info.json           (Script 02 output)
    data_cache/consolidated/{symbol}.parquet            (Script 03 output)

Outputs:
    data/performance/attribution/{date}_attribution.json
    reports/performance/attribution_{YYYY-MM}.pdf
    reports/performance/attribution_{YYYY-MM}.csv
    logs/performance_attribution_{timestamp}.log

CLI Reference:
    --start-date    YYYY-MM-DD  Period start (required unless --month)
    --end-date      YYYY-MM-DD  Period end (default: today)
    --month         YYYY-MM     Analyse a full calendar month (sets start/end)
    --benchmark     SPY.US | ACWI.US (default: SPY.US)
    --account-equity FLOAT      Current account equity in EUR (for VaR scaling)
    --dry-run       Validate and compute without writing files

Execution:
    # Monthly attribution (recommended)
    python scripts/22_performance_attribution.py \\
        --month 2026-01 \\
        --account-equity 52000

    # Custom date range
    python scripts/22_performance_attribution.py \\
        --start-date 2025-07-01 \\
        --end-date 2026-01-31 \\
        --benchmark ACWI.US \\
        --account-equity 52000

    # Dry run
    python scripts/22_performance_attribution.py \\
        --month 2026-01 \\
        --dry-run

Architecture: v3.2 (Feb 2026)
"""

import sys
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from config.strategies import resolve_strategies, add_strategy_argument, StrategyDef
import json
import logging
import argparse
import calendar
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ReportLab for PDF generation
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
DATA_LOAD_DIR = PROJECT_ROOT.parent / "data_load" / "data_cache"

CONSOLIDATED_DIR  = DATA_LOAD_DIR / "consolidated"
FUNDAMENTALS_DIR  = DATA_LOAD_DIR / "fundamentals"
PERF_DIR          = DATA_DIR / "performance" / "attribution"
REPORTS_DIR       = PROJECT_ROOT / "reports" / "performance"
LOG_DIR           = PROJECT_ROOT / "logs"

PORTFOLIO_STATE_FILE = DATA_DIR / "portfolio_state.json"
TRADE_LEDGER_FILE    = DATA_DIR / "trade_ledger.jsonl"
COMPANY_INFO_FILE    = FUNDAMENTALS_DIR / "company_info.json"

# ============================================================================
# DESIGN TOKENS  (matches Script 12 palette)
# ============================================================================

C_NAVY    = colors.HexColor("#1B2A47") if REPORTLAB_AVAILABLE else None
C_ACCENT  = colors.HexColor("#2E86DE") if REPORTLAB_AVAILABLE else None
C_SUCCESS = colors.HexColor("#27AE60") if REPORTLAB_AVAILABLE else None
C_DANGER  = colors.HexColor("#E74C3C") if REPORTLAB_AVAILABLE else None
C_ORANGE  = colors.HexColor("#E67E22") if REPORTLAB_AVAILABLE else None
C_HOLD    = colors.HexColor("#7F8C8D") if REPORTLAB_AVAILABLE else None
C_LIGHT   = colors.HexColor("#F8F9FA") if REPORTLAB_AVAILABLE else None
C_WHITE   = colors.white               if REPORTLAB_AVAILABLE else None

# ============================================================================
# ASSET CLASS CLASSIFICATION RULES
# ============================================================================

# Symbol suffix → asset class
SUFFIX_TO_ASSET_CLASS: Dict[str, str] = {
    ".US":  "US Stocks / ETFs",
    ".DE":  "EU Stocks (XETRA)",
    ".PA":  "EU Stocks (Euronext Paris)",
    ".AS":  "EU Stocks (Euronext Amsterdam)",
    ".L":   "UK Stocks (LSE)",
    ".CC":  "Cryptocurrency",
    ".V":   "Cryptocurrency",
}

# Known benchmark symbols that should be excluded from attribution
BENCHMARK_SYMBOLS = {"SPY.US", "ACWI.US", "QQQ.US", "IWM.US", "AGG.US"}

# ETF indicators in company_info (quoteType field from Yahoo Finance)
ETF_QUOTE_TYPES = {"ETF", "MUTUALFUND"}

# ============================================================================
# LOGGING SETUP
# ============================================================================

logger = logging.getLogger("performance_attribution")


def setup_logging(period_label: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"performance_attribution_{period_label}_{ts}.log"
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
class PositionAttribution:
    """Attribution record for a single instrument over the analysis period."""
    symbol:             str
    name:               str               = "Unknown"
    asset_class:        str               = "Unknown"
    sector:             str               = "Unknown"

    # P&L components (EUR)
    realized_pnl_eur:   float             = 0.0
    unrealized_pnl_eur: float             = 0.0
    total_pnl_eur:      float             = 0.0

    # Return metrics
    position_return_pct:  float           = 0.0   # return on cost basis
    portfolio_contribution_pct: float     = 0.0   # contribution to total portfolio return

    # Position sizing
    avg_weight_pct:     float             = 0.0   # avg weight during period (%)
    entry_price:        float             = 0.0
    exit_price:         float             = 0.0   # 0.0 if still open
    shares:             int               = 0

    # Trade metadata
    is_open:            bool              = True
    entry_date:         str               = ""
    exit_date:          str               = ""
    exit_reason:        str               = ""
    trades_count:       int               = 0


@dataclass
class AssetClassSummary:
    """Attribution aggregated by asset class."""
    asset_class:              str
    total_pnl_eur:            float = 0.0
    portfolio_contribution_pct: float = 0.0
    avg_weight_pct:           float = 0.0
    position_count:           int   = 0
    win_count:                int   = 0
    loss_count:               int   = 0

    @property
    def win_rate(self) -> float:
        total = self.win_count + self.loss_count
        return self.win_count / total * 100 if total > 0 else 0.0


@dataclass
class SectorSummary:
    """Attribution aggregated by sector."""
    sector:                   str
    total_pnl_eur:            float = 0.0
    portfolio_contribution_pct: float = 0.0
    avg_weight_pct:           float = 0.0
    position_count:           int   = 0
    win_count:                int   = 0
    loss_count:               int   = 0

    @property
    def win_rate(self) -> float:
        total = self.win_count + self.loss_count
        return self.win_count / total * 100 if total > 0 else 0.0


@dataclass
class AlphaBetaMetrics:
    """Alpha/beta decomposition vs benchmark."""
    benchmark_symbol:         str   = "SPY.US"
    benchmark_name:           str   = "S&P 500 (SPY)"

    # Returns
    portfolio_return_pct:     float = 0.0
    benchmark_return_pct:     float = 0.0
    active_return_pct:        float = 0.0

    # Regression metrics
    beta:                     float = 0.0
    alpha_annualized_pct:     float = 0.0
    r_squared:                float = 0.0

    # Risk metrics
    portfolio_volatility_ann: float = 0.0
    benchmark_volatility_ann: float = 0.0
    tracking_error_ann:       float = 0.0
    information_ratio:        float = 0.0
    sharpe_ratio:             float = 0.0   # portfolio, risk-free = 2%
    benchmark_sharpe:         float = 0.0

    # Return decomposition
    beta_contribution_pct:    float = 0.0   # = beta × benchmark_return
    alpha_contribution_pct:   float = 0.0   # = portfolio_return − beta_contribution

    # Data quality
    trading_days:             int   = 0
    data_coverage_pct:        float = 0.0   # % of days with price data for all positions


@dataclass
class AttributionReport:
    """Complete attribution report for the period."""
    period_start:             str
    period_end:               str
    period_label:             str   # e.g. "2026-01" or "2025-H2"
    generated_at:             str   = field(default_factory=lambda: datetime.now().isoformat())
    account_equity_eur:       float = 0.0

    # Period returns
    starting_portfolio_value: float = 0.0
    ending_portfolio_value:   float = 0.0
    total_return_pct:         float = 0.0
    total_pnl_eur:            float = 0.0

    # Trade statistics
    total_trades:             int   = 0
    closed_trades:            int   = 0
    open_positions:           int   = 0
    win_rate_pct:             float = 0.0
    avg_win_eur:              float = 0.0
    avg_loss_eur:             float = 0.0
    profit_factor:            float = 0.0

    # Attribution breakdowns
    positions:                List[PositionAttribution]  = field(default_factory=list)
    asset_classes:            List[AssetClassSummary]    = field(default_factory=list)
    sectors:                  List[SectorSummary]        = field(default_factory=list)
    alpha_beta:               Optional[AlphaBetaMetrics] = None

    # Top/bottom performers
    top_contributors:         List[Dict]                 = field(default_factory=list)
    top_detractors:           List[Dict]                 = field(default_factory=list)

    # Warnings
    warnings:                 List[str]                  = field(default_factory=list)


@dataclass
class MonthlyBreakdown:
    """
    Summary of a single calendar month, used inside the year report.

    Holds both the key headline numbers and a reference to the full
    AttributionReport for that month (used when rendering per-month
    detail sections in the year PDF).
    """
    month_label:              str    # "2026-01"
    month_name:               str    # "January 2026"
    month_start:              str    # ISO date
    month_end:                str    # ISO date
    is_complete:              bool   = True   # False for the current partial month

    # Headlines
    return_pct:               float  = 0.0
    pnl_eur:                  float  = 0.0
    win_rate_pct:             float  = 0.0
    profit_factor:            float  = 0.0
    open_positions:           int    = 0
    closed_trades:            int    = 0

    # Alpha / benchmark (populated when price data available)
    alpha_annualized_pct:     float  = 0.0
    benchmark_return_pct:     float  = 0.0
    active_return_pct:        float  = 0.0
    sharpe_ratio:             float  = 0.0
    information_ratio:        float  = 0.0

    # Best / worst position
    top_contributor_symbol:   str    = "—"
    top_contributor_pnl:      float  = 0.0
    top_detractor_symbol:     str    = "—"
    top_detractor_pnl:        float  = 0.0

    # Full report reference (None if computation failed / no data)
    report:                   Optional[AttributionReport] = field(
        default=None, compare=False, repr=False
    )


# ============================================================================
# DATA LOADERS
# ============================================================================

def load_trade_ledger(start_date: date, end_date: date) -> List[Dict]:
    """
    Load and filter trade_ledger.jsonl entries for the analysis period.

    Returns a list of trade dicts (all fields from Script 13).
    Both BUY and SELL entries are returned; callers filter by action.
    """
    if not TRADE_LEDGER_FILE.exists():
        logger.warning(f"Trade ledger not found: {TRADE_LEDGER_FILE}")
        return []

    trades = []
    with open(TRADE_LEDGER_FILE, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                trade_date_str = entry.get("execution_date", "")
                if not trade_date_str:
                    continue
                trade_date = date.fromisoformat(trade_date_str)
                if start_date <= trade_date <= end_date:
                    trades.append(entry)
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"Skipping malformed ledger line {line_no}: {e}")

    logger.info(f"  Loaded {len(trades)} trade(s) within period {start_date} → {end_date}")
    return trades


def load_all_trades() -> List[Dict]:
    """Load ALL trades from ledger (needed to find entry prices for open positions)."""
    if not TRADE_LEDGER_FILE.exists():
        return []
    trades = []
    with open(TRADE_LEDGER_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    trades.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return trades


def load_portfolio_state() -> Dict:
    """
    Load current portfolio_state.json.

    Returns normalised dict: {symbol: {entry_price, shares, asset_class, sector, ...}}
    """
    if not PORTFOLIO_STATE_FILE.exists():
        logger.warning("portfolio_state.json not found — no open positions loaded.")
        return {}

    with open(PORTFOLIO_STATE_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if not isinstance(raw, dict):
        logger.error("portfolio_state.json has unexpected format.")
        return {}

    # Support both flat and nested layouts
    if "positions" in raw and isinstance(raw.get("positions"), dict):
        raw = raw["positions"]

    positions = {k: v for k, v in raw.items() if not k.startswith("_")}
    logger.info(f"  Loaded {len(positions)} open position(s) from portfolio_state.json")
    return positions


def load_company_info() -> Dict:
    """
    Load company_info.json produced by Script 02.

    Returns {symbol: {name, sector, industry, quoteType, marketCap, ...}}
    """
    if not COMPANY_INFO_FILE.exists():
        logger.warning(f"company_info.json not found: {COMPANY_INFO_FILE}")
        return {}

    with open(COMPANY_INFO_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    logger.info(f"  Loaded company info for {len(data)} symbol(s)")
    return data


def load_price_series(symbol: str) -> Optional[pd.Series]:
    """
    Load closing price time series for a symbol from consolidated parquet.

    Returns a pandas Series indexed by date, or None if data unavailable.
    """
    parquet_path = CONSOLIDATED_DIR / f"{symbol}.parquet"
    if not parquet_path.exists():
        return None

    try:
        df = pd.read_parquet(parquet_path, columns=["close"])
        df.index = pd.to_datetime(df.index)
        return df["close"].sort_index()
    except Exception as e:
        logger.debug(f"  Could not load price data for {symbol}: {e}")
        return None


# ============================================================================
# CLASSIFICATION HELPERS
# ============================================================================

def classify_asset_class(symbol: str, company_info: Dict) -> str:
    """
    Determine asset class from symbol suffix and/or company info quoteType.

    Priority:
        1. Cryptocurrency suffix (.CC, .V)
        2. ETF flag from company_info.quoteType
        3. Geographic exchange suffix
        4. Fallback: "Unknown"
    """
    # 1. Crypto
    if symbol.endswith(".CC") or symbol.endswith(".V"):
        return "Cryptocurrency"

    # 2. ETF / Mutual Fund from company info
    info = company_info.get(symbol, {})
    quote_type = info.get("quoteType", "") or ""
    if quote_type.upper() in ETF_QUOTE_TYPES:
        return "ETF"

    # 3. Exchange suffix
    for suffix, asset_class in SUFFIX_TO_ASSET_CLASS.items():
        if symbol.upper().endswith(suffix.upper()):
            return asset_class

    return "Unknown"


def classify_sector(symbol: str, company_info: Dict) -> str:
    """Return sector from company_info, or 'Unknown'."""
    info = company_info.get(symbol, {})
    sector = info.get("sector") or info.get("industry") or ""
    return sector if sector else "Unknown"


def get_company_name(symbol: str, company_info: Dict) -> str:
    """Return company name from company_info, or the symbol itself."""
    info = company_info.get(symbol, {})
    return info.get("longName") or info.get("shortName") or symbol


# ============================================================================
# CORE ATTRIBUTION ENGINE
# ============================================================================

def compute_position_attribution(
    period_trades:    List[Dict],
    all_trades:       List[Dict],
    open_positions:   Dict,
    company_info:     Dict,
    start_date:       date,
    end_date:         date,
    starting_portfolio_value: float,
) -> List[PositionAttribution]:
    """
    Build a PositionAttribution record for every instrument touched during the period.

    Covers:
        ─ Closed positions (SELL in period): realized P&L from ledger
        ─ Open positions (still held at end of period): unrealized P&L = current price – entry
        ─ Positions opened AND closed within period: realized P&L only
    """
    results: Dict[str, PositionAttribution] = {}

    # ── Helper: get or create attribution record ──────────────────────────────
    def _get(sym: str) -> PositionAttribution:
        if sym not in results:
            info = company_info.get(sym, {})
            results[sym] = PositionAttribution(
                symbol      = sym,
                name        = get_company_name(sym, company_info),
                asset_class = classify_asset_class(sym, company_info),
                sector      = classify_sector(sym, company_info),
            )
        return results[sym]

    # ── STEP 1: Realized P&L from SELL trades in period ──────────────────────
    for trade in period_trades:
        if trade.get("action") != "SELL":
            continue

        sym = trade.get("symbol", "")
        if not sym or sym in BENCHMARK_SYMBOLS:
            continue

        attr = _get(sym)
        pnl = trade.get("realized_pnl_eur") or 0.0
        attr.realized_pnl_eur += pnl
        attr.is_open   = False
        attr.exit_date = trade.get("execution_date", "")
        attr.exit_price = trade.get("fill_price", 0.0)
        attr.shares    = trade.get("shares", 0)
        attr.exit_reason = trade.get("exit_reason") or ""
        attr.trades_count += 1

    # ── STEP 2: Entry prices from BUY trades (search all history) ────────────
    # Build a map: symbol → most recent BUY price & date (for open positions)
    buy_map: Dict[str, Dict] = {}
    for trade in sorted(all_trades, key=lambda t: t.get("execution_date", "")):
        if trade.get("action") == "BUY":
            sym = trade.get("symbol", "")
            if sym:
                buy_map[sym] = {
                    "entry_price": trade.get("fill_price", 0.0),
                    "entry_date":  trade.get("execution_date", ""),
                    "shares":      trade.get("shares", 0),
                }

    # ── STEP 3: Unrealized P&L for OPEN positions ────────────────────────────
    for sym, pos in open_positions.items():
        if sym in BENCHMARK_SYMBOLS:
            continue

        attr = _get(sym)
        attr.is_open = True

        # Entry price: prefer portfolio_state, fallback to last BUY in ledger
        entry_price = (
            pos.get("entry_price")
            or pos.get("avg_entry_price")
            or buy_map.get(sym, {}).get("entry_price", 0.0)
        )
        attr.entry_price = entry_price
        attr.entry_date  = (
            pos.get("entry_date")
            or buy_map.get(sym, {}).get("entry_date", "")
        )
        attr.shares = pos.get("shares", 0) or buy_map.get(sym, {}).get("shares", 0)

        # Current price: mark to the LAST AVAILABLE price on or before end_date.
        # This is the critical fix for the "identical months" bug — previously
        # iloc[-1] always returned today's live price, making Jan and Feb
        # attribution identical. Now January uses the Jan 31 close and
        # February uses the Feb 28 close (or latest available within each window).
        current_price = 0.0
        price_series = load_price_series(sym)
        if price_series is not None and len(price_series) > 0:
            end_ts = pd.Timestamp(end_date)
            sliced = price_series.loc[:end_ts]
            if len(sliced) > 0:
                current_price = float(sliced.iloc[-1])
            else:
                # No data up to end_date — fall back to earliest available
                current_price = float(price_series.iloc[0])
        # If parquet unavailable, try portfolio_state as last resort
        if current_price == 0.0:
            current_price = pos.get("current_price") or pos.get("last_price") or 0.0

        if entry_price > 0 and current_price > 0 and attr.shares > 0:
            # Unrealized P&L = (current − entry) × shares
            attr.unrealized_pnl_eur = round(
                (current_price - entry_price) * attr.shares, 4
            )
            attr.exit_price = current_price  # mark-to-market

        attr.trades_count += 1

    # ── STEP 4: Also handle BUY trades within period for new entries ──────────
    for trade in period_trades:
        if trade.get("action") != "BUY":
            continue
        sym = trade.get("symbol", "")
        if not sym or sym in BENCHMARK_SYMBOLS:
            continue
        attr = _get(sym)
        if attr.entry_price == 0.0:
            attr.entry_price = trade.get("fill_price", 0.0)
            attr.entry_date  = trade.get("execution_date", "")
        if attr.shares == 0:
            attr.shares = trade.get("shares", 0)
        attr.trades_count += 1

    # ── STEP 5: Compute totals and portfolio contribution ────────────────────
    for sym, attr in results.items():
        attr.total_pnl_eur = round(attr.realized_pnl_eur + attr.unrealized_pnl_eur, 4)

        # Position return % on cost basis
        cost_basis = attr.entry_price * attr.shares
        if cost_basis > 0:
            attr.position_return_pct = round(
                attr.total_pnl_eur / cost_basis * 100, 4
            )

        # Portfolio contribution % = P&L / Starting Portfolio Value
        if starting_portfolio_value > 0:
            attr.portfolio_contribution_pct = round(
                attr.total_pnl_eur / starting_portfolio_value * 100, 4
            )

        # Average weight (simplified: use position value / portfolio value)
        position_value = cost_basis if cost_basis > 0 else abs(attr.total_pnl_eur)
        if starting_portfolio_value > 0 and position_value > 0:
            attr.avg_weight_pct = round(
                position_value / starting_portfolio_value * 100, 2
            )

    return list(results.values())


def aggregate_by_asset_class(
    positions: List[PositionAttribution]
) -> List[AssetClassSummary]:
    """
    Roll up position-level attribution into asset class buckets.

    Contribution[asset_class] = Σ contribution[position] for positions in class.
    """
    buckets: Dict[str, AssetClassSummary] = {}

    for pos in positions:
        ac = pos.asset_class or "Unknown"
        if ac not in buckets:
            buckets[ac] = AssetClassSummary(asset_class=ac)
        b = buckets[ac]
        b.total_pnl_eur += pos.total_pnl_eur
        b.portfolio_contribution_pct += pos.portfolio_contribution_pct
        b.avg_weight_pct += pos.avg_weight_pct
        b.position_count += 1
        if pos.total_pnl_eur > 0:
            b.win_count += 1
        elif pos.total_pnl_eur < 0:
            b.loss_count += 1

    return sorted(buckets.values(), key=lambda x: x.total_pnl_eur, reverse=True)


def aggregate_by_sector(
    positions: List[PositionAttribution]
) -> List[SectorSummary]:
    """Roll up position-level attribution into sector buckets."""
    buckets: Dict[str, SectorSummary] = {}

    for pos in positions:
        sec = pos.sector or "Unknown"
        if sec not in buckets:
            buckets[sec] = SectorSummary(sector=sec)
        b = buckets[sec]
        b.total_pnl_eur += pos.total_pnl_eur
        b.portfolio_contribution_pct += pos.portfolio_contribution_pct
        b.avg_weight_pct += pos.avg_weight_pct
        b.position_count += 1
        if pos.total_pnl_eur > 0:
            b.win_count += 1
        elif pos.total_pnl_eur < 0:
            b.loss_count += 1

    return sorted(buckets.values(), key=lambda x: x.total_pnl_eur, reverse=True)


# ============================================================================
# ALPHA / BETA DECOMPOSITION
# ============================================================================

def reconstruct_portfolio_returns(
    positions:    List[PositionAttribution],
    open_positions: Dict,
    all_trades:   List[Dict],
    start_date:   date,
    end_date:     date,
) -> Optional[pd.Series]:
    """
    Reconstruct daily portfolio returns from constituent price histories.

    Method:
        1. For each position held during the period, load its closing price series
        2. Estimate daily portfolio value = Σ (shares × price)
        3. Compute daily returns: R[t] = (Value[t] - Value[t-1]) / Value[t-1]

    Returns a pandas Series of daily returns indexed by date, or None if
    insufficient price data is available (< 20 trading days).
    """
    start_ts = pd.Timestamp(start_date)
    end_ts   = pd.Timestamp(end_date)

    # Build symbol → shares map from portfolio state and ledger
    symbol_shares: Dict[str, int] = {}
    for pos in positions:
        if pos.shares > 0:
            symbol_shares[pos.symbol] = pos.shares

    if not symbol_shares:
        logger.warning("No position data to reconstruct portfolio returns.")
        return None

    # Load price series for each symbol
    price_frames = {}
    for sym in symbol_shares:
        if sym in BENCHMARK_SYMBOLS:
            continue
        series = load_price_series(sym)
        if series is not None:
            filtered = series.loc[start_ts:end_ts]
            if len(filtered) >= 5:
                price_frames[sym] = filtered

    if len(price_frames) < 1:
        logger.warning("Insufficient price data for portfolio return reconstruction.")
        return None

    # Align all series to a common business-day index
    price_df = pd.DataFrame(price_frames).sort_index()
    price_df = price_df.ffill()                        # fill gaps with prior close
    price_df = price_df.loc[start_ts:end_ts]

    if len(price_df) < 5:
        return None

    # Weight portfolio value by shares
    shares_series = pd.Series({
        sym: symbol_shares[sym]
        for sym in price_df.columns
    })

    # Daily portfolio value = sum(shares × price)
    portfolio_value = price_df.mul(shares_series, axis=1).sum(axis=1)
    portfolio_value = portfolio_value[portfolio_value > 0]

    if len(portfolio_value) < 5:
        return None

    daily_returns = portfolio_value.pct_change().dropna()
    logger.info(
        f"  Portfolio return series: {len(daily_returns)} trading days, "
        f"coverage {len(price_frames)}/{len(symbol_shares)} positions"
    )
    return daily_returns


def compute_alpha_beta(
    portfolio_returns: pd.Series,
    benchmark_symbol:  str,
    start_date:        date,
    end_date:          date,
    risk_free_rate:    float = 0.02,    # 2% annual risk-free rate
) -> AlphaBetaMetrics:
    """
    Compute full alpha/beta decomposition vs benchmark.

    Formulas:
        Beta  = Cov(Rp, Rb) / Var(Rb)
        Alpha = Annualized(mean(Rp) - beta × mean(Rb))
              = (mean(Rp) - beta × mean(Rb)) × 252
        Tracking Error = StdDev(Rp - Rb) × sqrt(252)
        Information Ratio = Active_Return / Tracking_Error
        Sharpe = (Rp_ann - RFR) / Vol_ann

    where RFR = risk_free_rate / 252 (daily)
    """
    metrics = AlphaBetaMetrics(benchmark_symbol=benchmark_symbol)

    # Load benchmark returns
    benchmark_prices = load_price_series(benchmark_symbol)

    if benchmark_prices is None:
        logger.warning(f"Benchmark {benchmark_symbol} price data not available.")
        metrics.portfolio_return_pct = float(
            (portfolio_returns + 1).prod() - 1
        ) * 100
        return metrics

    start_ts = pd.Timestamp(start_date)
    end_ts   = pd.Timestamp(end_date)
    benchmark_prices = benchmark_prices.loc[start_ts:end_ts]

    if len(benchmark_prices) < 5:
        logger.warning(f"Insufficient benchmark data for {benchmark_symbol}.")
        return metrics

    benchmark_returns = benchmark_prices.pct_change().dropna()

    # Align on common dates
    common_idx = portfolio_returns.index.intersection(benchmark_returns.index)
    if len(common_idx) < 10:
        logger.warning(
            f"Only {len(common_idx)} common trading days — alpha/beta unreliable."
        )
        metrics.data_coverage_pct = len(common_idx) / max(len(portfolio_returns), 1) * 100
        return metrics

    rp = portfolio_returns.loc[common_idx]
    rb = benchmark_returns.loc[common_idx]
    n  = len(common_idx)

    # ── Beta via OLS ──────────────────────────────────────────────────────────
    cov_matrix  = np.cov(rp.values, rb.values)
    var_bench   = np.var(rb.values, ddof=1)
    beta        = cov_matrix[0, 1] / var_bench if var_bench > 1e-10 else 1.0

    # ── Annualized returns ────────────────────────────────────────────────────
    rp_ann = float((rp + 1).prod() ** (252 / n) - 1) * 100  # percent
    rb_ann = float((rb + 1).prod() ** (252 / n) - 1) * 100

    # ── R-squared ────────────────────────────────────────────────────────────
    corr   = np.corrcoef(rp.values, rb.values)[0, 1]
    r_sq   = corr ** 2

    # ── Alpha (annualized, percent) ───────────────────────────────────────────
    # α = E[Rp] - β × E[Rb]  (daily basis), then annualize
    alpha_daily = float(np.mean(rp.values) - beta * np.mean(rb.values))
    alpha_ann   = alpha_daily * 252 * 100  # percent

    # ── Volatility (annualized, percent) ─────────────────────────────────────
    vol_p = float(np.std(rp.values, ddof=1)) * np.sqrt(252) * 100
    vol_b = float(np.std(rb.values, ddof=1)) * np.sqrt(252) * 100

    # ── Tracking Error (annualized, percent) ─────────────────────────────────
    active_daily = rp.values - rb.values
    te_ann       = float(np.std(active_daily, ddof=1)) * np.sqrt(252) * 100

    # ── Information Ratio ────────────────────────────────────────────────────
    active_return = rp_ann - rb_ann
    ir            = active_return / te_ann if te_ann > 1e-6 else 0.0

    # ── Sharpe Ratios ─────────────────────────────────────────────────────────
    rfr_pct  = risk_free_rate * 100
    sharpe_p = (rp_ann - rfr_pct) / vol_p if vol_p > 1e-6 else 0.0
    sharpe_b = (rb_ann - rfr_pct) / vol_b if vol_b > 1e-6 else 0.0

    # ── Period returns (not annualized, for display) ──────────────────────────
    port_period_return = float((rp + 1).prod() - 1) * 100
    bench_period_return = float((rb + 1).prod() - 1) * 100
    beta_contribution  = beta * bench_period_return
    alpha_contribution = port_period_return - beta_contribution

    # Populate metrics
    metrics.portfolio_return_pct     = round(port_period_return, 4)
    metrics.benchmark_return_pct     = round(bench_period_return, 4)
    metrics.active_return_pct        = round(port_period_return - bench_period_return, 4)
    metrics.beta                     = round(beta, 4)
    metrics.alpha_annualized_pct     = round(alpha_ann, 4)
    metrics.r_squared                = round(r_sq, 4)
    metrics.portfolio_volatility_ann = round(vol_p, 4)
    metrics.benchmark_volatility_ann = round(vol_b, 4)
    metrics.tracking_error_ann       = round(te_ann, 4)
    metrics.information_ratio        = round(ir, 4)
    metrics.sharpe_ratio             = round(sharpe_p, 4)
    metrics.benchmark_sharpe         = round(sharpe_b, 4)
    metrics.beta_contribution_pct    = round(beta_contribution, 4)
    metrics.alpha_contribution_pct   = round(alpha_contribution, 4)
    metrics.trading_days             = n
    metrics.data_coverage_pct        = round(n / max(len(portfolio_returns), 1) * 100, 1)

    # Benchmark human-readable name
    BENCHMARK_NAMES = {
        "SPY.US":  "S&P 500 (SPY)",
        "ACWI.US": "MSCI All-Country World (ACWI)",
        "QQQ.US":  "NASDAQ-100 (QQQ)",
    }
    metrics.benchmark_name = BENCHMARK_NAMES.get(benchmark_symbol, benchmark_symbol)

    logger.info(
        f"  Alpha/Beta: β={beta:.3f}, α={alpha_ann:.2f}% ann., "
        f"IR={ir:.3f}, TE={te_ann:.2f}%"
    )
    return metrics


# ============================================================================
# TRADE STATISTICS
# ============================================================================

def compute_trade_statistics(
    positions: List[PositionAttribution],
) -> Dict:
    """
    Compute aggregate trade statistics: win rate, profit factor, averages.

    Win  = total_pnl_eur > 0
    Loss = total_pnl_eur < 0
    """
    wins   = [p.total_pnl_eur for p in positions if p.total_pnl_eur > 0]
    losses = [p.total_pnl_eur for p in positions if p.total_pnl_eur < 0]
    total  = len(positions)

    gross_profit = sum(wins)
    gross_loss   = abs(sum(losses))

    return {
        "total_positions":  total,
        "win_count":        len(wins),
        "loss_count":       len(losses),
        "win_rate_pct":     round(len(wins) / total * 100, 2) if total > 0 else 0.0,
        "avg_win_eur":      round(np.mean(wins), 2)           if wins   else 0.0,
        "avg_loss_eur":     round(np.mean(losses), 2)         if losses else 0.0,
        "gross_profit_eur": round(gross_profit, 2),
        "gross_loss_eur":   round(gross_loss, 2),
        "profit_factor":    round(gross_profit / gross_loss, 4) if gross_loss > 1e-6 else float("inf"),
        "largest_win_eur":  round(max(wins), 2)                if wins   else 0.0,
        "largest_loss_eur": round(min(losses), 2)              if losses else 0.0,
        "expectancy_eur":   round(
            (len(wins) / total * np.mean(wins) + len(losses) / total * np.mean(losses))
            if total > 0 and (wins or losses) else 0.0,
            2,
        ),
    }


# ============================================================================
# REPORT ASSEMBLER
# ============================================================================

def assemble_report(
    positions:              List[PositionAttribution],
    asset_classes:          List[AssetClassSummary],
    sectors:                List[SectorSummary],
    alpha_beta:             Optional[AlphaBetaMetrics],
    period_trades:          List[Dict],
    open_positions:         Dict,
    start_date:             date,
    end_date:               date,
    period_label:           str,
    account_equity_eur:     float,
    warnings:               List[str],
) -> AttributionReport:
    """Assemble all computed data into the final AttributionReport."""

    # ── P&L totals ────────────────────────────────────────────────────────────
    total_realized   = sum(p.realized_pnl_eur   for p in positions)
    total_unrealized = sum(p.unrealized_pnl_eur for p in positions)
    total_pnl        = total_realized + total_unrealized

    # Starting portfolio value = sum of cost bases for all positions in period
    cost_bases = [p.entry_price * p.shares for p in positions if p.entry_price > 0 and p.shares > 0]
    starting_value = sum(cost_bases) if cost_bases else account_equity_eur

    total_return_pct = (total_pnl / starting_value * 100) if starting_value > 0 else 0.0

    # ── Trade statistics ──────────────────────────────────────────────────────
    stats = compute_trade_statistics(positions)

    # ── Top / bottom performers ───────────────────────────────────────────────
    sorted_by_contribution = sorted(
        positions, key=lambda p: p.total_pnl_eur, reverse=True
    )
    top_n = 10

    top_contributors = [
        {
            "rank":                   i + 1,
            "symbol":                 p.symbol,
            "name":                   p.name,
            "sector":                 p.sector,
            "asset_class":            p.asset_class,
            "total_pnl_eur":          round(p.total_pnl_eur, 2),
            "portfolio_contribution_pct": round(p.portfolio_contribution_pct, 3),
            "position_return_pct":    round(p.position_return_pct, 2),
            "status":                 "OPEN" if p.is_open else "CLOSED",
            "exit_reason":            p.exit_reason,
        }
        for i, p in enumerate(sorted_by_contribution[:top_n])
        if p.total_pnl_eur > 0
    ]

    top_detractors = [
        {
            "rank":                   i + 1,
            "symbol":                 p.symbol,
            "name":                   p.name,
            "sector":                 p.sector,
            "asset_class":            p.asset_class,
            "total_pnl_eur":          round(p.total_pnl_eur, 2),
            "portfolio_contribution_pct": round(p.portfolio_contribution_pct, 3),
            "position_return_pct":    round(p.position_return_pct, 2),
            "status":                 "OPEN" if p.is_open else "CLOSED",
            "exit_reason":            p.exit_reason,
        }
        for i, p in enumerate(reversed(sorted_by_contribution[-top_n:]))
        if p.total_pnl_eur < 0
    ]

    report = AttributionReport(
        period_start             = start_date.isoformat(),
        period_end               = end_date.isoformat(),
        period_label             = period_label,
        account_equity_eur       = account_equity_eur,
        starting_portfolio_value = round(starting_value, 2),
        ending_portfolio_value   = round(starting_value + total_pnl, 2),
        total_return_pct         = round(total_return_pct, 4),
        total_pnl_eur            = round(total_pnl, 2),
        total_trades             = stats["total_positions"],
        closed_trades            = sum(1 for p in positions if not p.is_open),
        open_positions           = sum(1 for p in positions if p.is_open),
        win_rate_pct             = stats["win_rate_pct"],
        avg_win_eur              = stats["avg_win_eur"],
        avg_loss_eur             = stats["avg_loss_eur"],
        profit_factor            = stats["profit_factor"],
        positions                = sorted_by_contribution,
        asset_classes            = asset_classes,
        sectors                  = sectors,
        alpha_beta               = alpha_beta,
        top_contributors         = top_contributors,
        top_detractors           = top_detractors,
        warnings                 = warnings,
    )
    return report


# ============================================================================
# JSON EXPORT
# ============================================================================

def export_json(report: AttributionReport, dry_run: bool = False) -> Optional[Path]:
    """Serialise the attribution report to JSON."""
    PERF_DIR.mkdir(parents=True, exist_ok=True)
    out_path = PERF_DIR / f"{report.period_end}_{report.period_label}_attribution.json"

    # Convert dataclasses to plain dicts for JSON serialisation
    payload = {
        "period_start":             report.period_start,
        "period_end":               report.period_end,
        "period_label":             report.period_label,
        "generated_at":             report.generated_at,
        "account_equity_eur":       report.account_equity_eur,
        "starting_portfolio_value": report.starting_portfolio_value,
        "ending_portfolio_value":   report.ending_portfolio_value,
        "total_return_pct":         report.total_return_pct,
        "total_pnl_eur":            report.total_pnl_eur,
        "trade_statistics": {
            "total_trades":   report.total_trades,
            "closed_trades":  report.closed_trades,
            "open_positions": report.open_positions,
            "win_rate_pct":   report.win_rate_pct,
            "avg_win_eur":    report.avg_win_eur,
            "avg_loss_eur":   report.avg_loss_eur,
            "profit_factor":  report.profit_factor,
        },
        "asset_class_attribution": [asdict(ac) for ac in report.asset_classes],
        "sector_attribution":      [asdict(s)  for s  in report.sectors],
        "position_attribution":    [asdict(p)  for p  in report.positions],
        "alpha_beta":              asdict(report.alpha_beta) if report.alpha_beta else None,
        "top_contributors":        report.top_contributors,
        "top_detractors":          report.top_detractors,
        "warnings":                report.warnings,
    }

    if dry_run:
        logger.info(f"  [DRY-RUN] Would write JSON → {out_path}")
        return out_path

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)

    logger.info(f"  JSON attribution → {out_path}")
    return out_path


def export_csv(report: AttributionReport, dry_run: bool = False) -> Optional[Path]:
    """Export position-level attribution as CSV for spreadsheet review."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"attribution_{report.period_label}.csv"

    rows = []
    for p in report.positions:
        rows.append({
            "Symbol":              p.symbol,
            "Name":                p.name,
            "Asset Class":         p.asset_class,
            "Sector":              p.sector,
            "Status":              "OPEN" if p.is_open else "CLOSED",
            "Entry Date":          p.entry_date,
            "Exit Date":           p.exit_date if not p.is_open else "",
            "Entry Price (EUR)":   round(p.entry_price, 4),
            "Exit Price (EUR)":    round(p.exit_price, 4) if p.exit_price else "",
            "Shares":              p.shares,
            "Realized P&L (EUR)":  round(p.realized_pnl_eur, 2),
            "Unrealized P&L (EUR)": round(p.unrealized_pnl_eur, 2),
            "Total P&L (EUR)":     round(p.total_pnl_eur, 2),
            "Position Return (%)": round(p.position_return_pct, 2),
            "Portfolio Contribution (%)": round(p.portfolio_contribution_pct, 3),
            "Avg Weight (%)":      round(p.avg_weight_pct, 2),
            "Exit Reason":         p.exit_reason,
        })

    if dry_run:
        logger.info(f"  [DRY-RUN] Would write CSV → {out_path}")
        return out_path

    import csv
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        if rows:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

    logger.info(f"  CSV attribution → {out_path}")
    return out_path


# ============================================================================
# PDF REPORT GENERATOR
# ============================================================================

def _pct_color(val: float) -> object:
    """Return green color for positive, red for negative, grey for zero."""
    if val > 0:
        return C_SUCCESS
    if val < 0:
        return C_DANGER
    return C_HOLD


def _fmt_eur(val: float) -> str:
    """Format float as EUR with sign and 2 decimal places."""
    sign = "+" if val > 0 else ""
    return f"{sign}€{val:,.2f}"


def _fmt_pct(val: float, decimals: int = 2) -> str:
    """Format float as percentage with sign."""
    sign = "+" if val > 0 else ""
    return f"{sign}{val:.{decimals}f}%"


def _fmt_price(val: float) -> str:
    """Format a price with up to 4 significant decimal places."""
    if val == 0:
        return "—"
    if val < 1:
        return f"€{val:.4f}"
    if val < 10:
        return f"€{val:.3f}"
    return f"€{val:,.2f}"


def _table_style_base() -> List:
    """Base table style shared by all tables in the report."""
    return [
        ("BACKGROUND",   (0, 0), (-1, 0), C_NAVY),
        ("TEXTCOLOR",    (0, 0), (-1, 0), C_WHITE),
        ("FONTNAME",     (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",     (0, 0), (-1, 0), 8),
        ("FONTSIZE",     (0, 1), (-1, -1), 7.5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [C_LIGHT, C_WHITE]),
        ("GRID",         (0, 0), (-1, -1), 0.3, C_HOLD),
        ("LEFTPADDING",  (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING",   (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
    ]


def generate_pdf(
    report: AttributionReport,
    dry_run: bool = False,
) -> Optional[Path]:
    """Generate a professional PDF attribution report using ReportLab."""

    if not REPORTLAB_AVAILABLE:
        logger.warning("ReportLab not installed — skipping PDF generation.")
        logger.warning("Install with: pip install reportlab")
        return None

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"attribution_{report.period_label}.pdf"

    if dry_run:
        logger.info(f"  [DRY-RUN] Would write PDF → {out_path}")
        return out_path

    PAGE_W, PAGE_H = A4
    MARGIN_H = 18 * mm
    MARGIN_V = 22 * mm
    CONTENT_W = PAGE_W - 2 * MARGIN_H

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=MARGIN_H, rightMargin=MARGIN_H,
        topMargin=MARGIN_V, bottomMargin=MARGIN_V,
        title=f"Performance Attribution — {report.period_label}",
    )

    styles = getSampleStyleSheet()
    H1 = ParagraphStyle("H1", fontSize=16, fontName="Helvetica-Bold",
                         textColor=C_NAVY, spaceBefore=6, spaceAfter=4)
    H2 = ParagraphStyle("H2", fontSize=12, fontName="Helvetica-Bold",
                         textColor=C_ACCENT, spaceBefore=10, spaceAfter=4)
    BODY = ParagraphStyle("Body", fontSize=8.5, fontName="Helvetica",
                           textColor=colors.black, spaceAfter=2)
    SMALL = ParagraphStyle("Small", fontSize=7.5, fontName="Helvetica",
                            textColor=C_HOLD, spaceAfter=2)
    BOLD = ParagraphStyle("Bold", fontSize=8.5, fontName="Helvetica-Bold",
                           textColor=colors.black)

    story = []

    # ── Cover / Header ────────────────────────────────────────────────────────
    story.append(Paragraph(
        f"Performance Attribution Report",
        H1,
    ))
    story.append(Paragraph(
        f"Period: {report.period_start}  →  {report.period_end}  |  "
        f"Generated: {report.generated_at[:10]}",
        SMALL,
    ))
    story.append(HRFlowable(width=CONTENT_W, thickness=1.5, color=C_NAVY))
    story.append(Spacer(1, 4 * mm))

    # ── Executive Summary Banner ──────────────────────────────────────────────
    return_color = C_SUCCESS if report.total_return_pct >= 0 else C_DANGER

    summary_data = [
        ["Period Return", "Total P&L (EUR)", "Win Rate", "Profit Factor",
         "Open Positions", "Closed Positions"],
        [
            _fmt_pct(report.total_return_pct),
            _fmt_eur(report.total_pnl_eur),
            f"{report.win_rate_pct:.1f}%",
            f"{report.profit_factor:.2f}x" if report.profit_factor != float("inf") else "∞",
            str(report.open_positions),
            str(report.closed_trades),
        ],
    ]
    summary_tbl = Table(summary_data, colWidths=[CONTENT_W / 6] * 6)
    summary_tbl.setStyle(TableStyle([
        ("BACKGROUND",  (0, 0), (-1, 0), C_NAVY),
        ("TEXTCOLOR",   (0, 0), (-1, 0), C_WHITE),
        ("FONTNAME",    (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",    (0, 0), (-1, 0), 8),
        ("FONTSIZE",    (0, 1), (-1, 1), 11),
        ("FONTNAME",    (0, 1), (-1, 1), "Helvetica-Bold"),
        ("TEXTCOLOR",   (0, 1), (0, 1), return_color),
        ("TEXTCOLOR",   (1, 1), (1, 1), return_color),
        ("TEXTCOLOR",   (2, 1), (-1, 1), C_ACCENT),
        ("ALIGN",       (0, 0), (-1, -1), "CENTER"),
        ("GRID",        (0, 0), (-1, -1), 0.3, C_HOLD),
        ("TOPPADDING",  (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(summary_tbl)
    story.append(Spacer(1, 6 * mm))

    # ── Alpha / Beta Section ──────────────────────────────────────────────────
    if report.alpha_beta:
        ab = report.alpha_beta
        story.append(Paragraph("Alpha / Beta Decomposition", H2))

        ab_data = [
            ["Metric", "Portfolio", "Benchmark", "Interpretation"],
            [
                "Period Return",
                _fmt_pct(ab.portfolio_return_pct),
                _fmt_pct(ab.benchmark_return_pct),
                f"Active Return: {_fmt_pct(ab.active_return_pct)}",
            ],
            [
                "Annualized Volatility",
                f"{ab.portfolio_volatility_ann:.2f}%",
                f"{ab.benchmark_volatility_ann:.2f}%",
                "Lower = less risk",
            ],
            [
                "Sharpe Ratio",
                f"{ab.sharpe_ratio:.3f}",
                f"{ab.benchmark_sharpe:.3f}",
                "> 1.0 = excellent",
            ],
            [
                "Beta (β)",
                f"{ab.beta:.3f}",
                "1.000",
                "< 1 = lower market sensitivity",
            ],
            [
                "Alpha (α, annualized)",
                _fmt_pct(ab.alpha_annualized_pct),
                "—",
                "> 3% = target achieved",
            ],
            [
                "Tracking Error (ann.)",
                f"{ab.tracking_error_ann:.2f}%",
                "—",
                "5–10% = moderate active risk",
            ],
            [
                "Information Ratio",
                f"{ab.information_ratio:.3f}",
                "—",
                "> 0.50 = good active management",
            ],
            [
                "R-Squared",
                f"{ab.r_squared:.3f}",
                "—",
                f"{ab.trading_days} trading days analyzed",
            ],
        ]

        ab_widths = [CONTENT_W * 0.28, CONTENT_W * 0.18, CONTENT_W * 0.18, CONTENT_W * 0.36]
        ab_tbl = Table(ab_data, colWidths=ab_widths)
        ab_style = _table_style_base()
        # Colour alpha row
        alpha_row = 5
        alpha_val = ab.alpha_annualized_pct
        ab_style.append(("TEXTCOLOR", (1, alpha_row), (1, alpha_row),
                          C_SUCCESS if alpha_val >= 0 else C_DANGER))
        ab_style.append(("FONTNAME", (1, alpha_row), (1, alpha_row), "Helvetica-Bold"))
        ab_tbl.setStyle(TableStyle(ab_style))
        story.append(ab_tbl)

        # Return decomposition note
        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph(
            f"Return Decomposition: Total {_fmt_pct(ab.portfolio_return_pct)} = "
            f"Beta Contribution {_fmt_pct(ab.beta_contribution_pct)} "
            f"(β={ab.beta:.2f} × Benchmark {_fmt_pct(ab.benchmark_return_pct)}) "
            f"+ Alpha {_fmt_pct(ab.alpha_contribution_pct)} | "
            f"Benchmark: {ab.benchmark_name}",
            SMALL,
        ))
        story.append(Spacer(1, 5 * mm))

    # ── Asset Class Attribution ───────────────────────────────────────────────
    story.append(Paragraph("Attribution by Asset Class", H2))
    ac_header = ["Asset Class", "Positions", "Total P&L (EUR)", "Contribution (%)",
                  "Avg Weight (%)", "Win Rate"]
    ac_rows   = [ac_header]
    for ac in report.asset_classes:
        ac_rows.append([
            ac.asset_class,
            str(ac.position_count),
            _fmt_eur(ac.total_pnl_eur),
            _fmt_pct(ac.portfolio_contribution_pct, 3),
            f"{ac.avg_weight_pct:.1f}%",
            f"{ac.win_rate:.0f}%",
        ])

    ac_widths = [CONTENT_W * 0.32, CONTENT_W * 0.10, CONTENT_W * 0.18,
                 CONTENT_W * 0.18, CONTENT_W * 0.12, CONTENT_W * 0.10]
    ac_tbl = Table(ac_rows, colWidths=ac_widths)
    ac_style = _table_style_base()
    for row_idx, ac in enumerate(report.asset_classes, start=1):
        color = C_SUCCESS if ac.total_pnl_eur >= 0 else C_DANGER
        ac_style.append(("TEXTCOLOR", (2, row_idx), (3, row_idx), color))
        ac_style.append(("FONTNAME",  (2, row_idx), (3, row_idx), "Helvetica-Bold"))
    ac_tbl.setStyle(TableStyle(ac_style))
    story.append(ac_tbl)
    story.append(Spacer(1, 5 * mm))

    # ── Sector Attribution ────────────────────────────────────────────────────
    story.append(Paragraph("Attribution by Sector", H2))
    sec_header = ["Sector", "Positions", "Total P&L (EUR)", "Contribution (%)",
                   "Avg Weight (%)", "Win Rate"]
    sec_rows = [sec_header]
    for sec in report.sectors[:15]:   # top 15 sectors
        sec_rows.append([
            sec.sector,
            str(sec.position_count),
            _fmt_eur(sec.total_pnl_eur),
            _fmt_pct(sec.portfolio_contribution_pct, 3),
            f"{sec.avg_weight_pct:.1f}%",
            f"{sec.win_rate:.0f}%",
        ])

    sec_widths = [CONTENT_W * 0.32, CONTENT_W * 0.10, CONTENT_W * 0.18,
                  CONTENT_W * 0.18, CONTENT_W * 0.12, CONTENT_W * 0.10]
    sec_tbl = Table(sec_rows, colWidths=sec_widths)
    sec_style = _table_style_base()
    for row_idx, sec in enumerate(report.sectors[:15], start=1):
        color = C_SUCCESS if sec.total_pnl_eur >= 0 else C_DANGER
        sec_style.append(("TEXTCOLOR", (2, row_idx), (3, row_idx), color))
        sec_style.append(("FONTNAME",  (2, row_idx), (3, row_idx), "Helvetica-Bold"))
    sec_tbl.setStyle(TableStyle(sec_style))
    story.append(sec_tbl)
    story.append(Spacer(1, 5 * mm))

    # ── Top Contributors ──────────────────────────────────────────────────────
    story.append(Paragraph("Top Contributors", H2))
    if report.top_contributors:
        contrib_header = ["#", "Symbol", "Name", "Sector", "P&L (EUR)",
                          "Contribution (%)", "Position Return (%)", "Status"]
        contrib_rows = [contrib_header]
        for c in report.top_contributors[:10]:
            contrib_rows.append([
                str(c["rank"]),
                c["symbol"],
                c["name"][:25],
                c["sector"][:18],
                _fmt_eur(c["total_pnl_eur"]),
                _fmt_pct(c["portfolio_contribution_pct"], 3),
                _fmt_pct(c["position_return_pct"]),
                c["status"],
            ])
        ct_widths = [
            CONTENT_W * 0.04, CONTENT_W * 0.10, CONTENT_W * 0.19,
            CONTENT_W * 0.16, CONTENT_W * 0.14, CONTENT_W * 0.13,
            CONTENT_W * 0.14, CONTENT_W * 0.10,
        ]
        ct_tbl = Table(contrib_rows, colWidths=ct_widths)
        ct_style = _table_style_base()
        for row_idx in range(1, len(contrib_rows)):
            ct_style.append(("TEXTCOLOR", (4, row_idx), (6, row_idx), C_SUCCESS))
            ct_style.append(("FONTNAME",  (4, row_idx), (5, row_idx), "Helvetica-Bold"))
        ct_tbl.setStyle(TableStyle(ct_style))
        story.append(ct_tbl)
    else:
        story.append(Paragraph("No positive contributors in this period.", BODY))
    story.append(Spacer(1, 4 * mm))

    # ── Top Detractors ────────────────────────────────────────────────────────
    story.append(Paragraph("Top Detractors", H2))
    if report.top_detractors:
        det_header = ["#", "Symbol", "Name", "Sector", "P&L (EUR)",
                       "Contribution (%)", "Position Return (%)", "Exit Reason"]
        det_rows = [det_header]
        for d in report.top_detractors[:10]:
            det_rows.append([
                str(d["rank"]),
                d["symbol"],
                d["name"][:25],
                d["sector"][:18],
                _fmt_eur(d["total_pnl_eur"]),
                _fmt_pct(d["portfolio_contribution_pct"], 3),
                _fmt_pct(d["position_return_pct"]),
                (d.get("exit_reason") or "—")[:20],
            ])
        dt_widths = [
            CONTENT_W * 0.04, CONTENT_W * 0.10, CONTENT_W * 0.18,
            CONTENT_W * 0.16, CONTENT_W * 0.14, CONTENT_W * 0.13,
            CONTENT_W * 0.14, CONTENT_W * 0.11,
        ]
        dt_tbl = Table(det_rows, colWidths=dt_widths)
        dt_style = _table_style_base()
        for row_idx in range(1, len(det_rows)):
            dt_style.append(("TEXTCOLOR", (4, row_idx), (6, row_idx), C_DANGER))
            dt_style.append(("FONTNAME",  (4, row_idx), (5, row_idx), "Helvetica-Bold"))
        dt_tbl.setStyle(TableStyle(dt_style))
        story.append(dt_tbl)
    else:
        story.append(Paragraph("No detractors (all positions profitable) in this period.", BODY))
    story.append(Spacer(1, 4 * mm))

    # ── Full Position Table ───────────────────────────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph("Full Position-Level Attribution", H2))
    pos_header = [
        "Symbol", "Name", "Asset Class", "Sector", "Status",
        "Realized\nP&L (EUR)", "Unrealized\nP&L (EUR)",
        "Total\nP&L (EUR)", "Pos. Return\n(%)", "Portfolio\nContrib (%)",
    ]
    pos_rows = [pos_header]
    for p in report.positions:
        pos_rows.append([
            p.symbol,
            p.name[:20],
            p.asset_class[:18],
            p.sector[:18],
            "OPEN" if p.is_open else "CLOSED",
            _fmt_eur(p.realized_pnl_eur),
            _fmt_eur(p.unrealized_pnl_eur),
            _fmt_eur(p.total_pnl_eur),
            _fmt_pct(p.position_return_pct),
            _fmt_pct(p.portfolio_contribution_pct, 3),
        ])

    pos_col_widths = [
        CONTENT_W * 0.09, CONTENT_W * 0.16, CONTENT_W * 0.14,
        CONTENT_W * 0.14, CONTENT_W * 0.07,
        CONTENT_W * 0.10, CONTENT_W * 0.10,
        CONTENT_W * 0.10, CONTENT_W * 0.10, CONTENT_W * 0.10,
    ]
    pos_tbl = Table(pos_rows, colWidths=pos_col_widths, repeatRows=1)
    pos_style = _table_style_base()
    for row_idx, p in enumerate(report.positions, start=1):
        total_col = 7
        color = C_SUCCESS if p.total_pnl_eur >= 0 else C_DANGER
        pos_style.append(("TEXTCOLOR", (total_col, row_idx), (total_col + 1, row_idx), color))
    pos_tbl.setStyle(TableStyle(pos_style))
    story.append(pos_tbl)
    story.append(Spacer(1, 4 * mm))

    # ── Warnings ──────────────────────────────────────────────────────────────
    if report.warnings:
        story.append(Paragraph("Warnings", H2))
        for w in report.warnings:
            story.append(Paragraph(f"⚠  {w}", BODY))

    # ── Footer ────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 6 * mm))
    story.append(HRFlowable(width=CONTENT_W, thickness=0.5, color=C_HOLD))
    story.append(Paragraph(
        f"Multi-Asset Trend Following Strategy — Architecture v3.2 (Feb 2026)  |  "
        f"Script 22: Performance Attribution  |  "
        f"Period: {report.period_label}  |  Generated: {report.generated_at[:19]}",
        SMALL,
    ))

    doc.build(story)
    logger.info(f"  PDF attribution → {out_path}")
    return out_path


# ============================================================================
# CONSOLE PRINT SUMMARY
# ============================================================================

def print_summary(report: AttributionReport) -> None:
    """Print a concise attribution summary to the console."""
    SEP = "=" * 72
    sep = "-" * 72
    print(f"\n{SEP}")
    print(f"  PERFORMANCE ATTRIBUTION — {report.period_label}")
    print(f"  Period: {report.period_start}  →  {report.period_end}")
    print(SEP)
    print(f"  Total Return   : {_fmt_pct(report.total_return_pct)}")
    print(f"  Total P&L      : {_fmt_eur(report.total_pnl_eur)}")
    print(f"  Starting NAV   : €{report.starting_portfolio_value:,.2f}")
    print(f"  Ending NAV     : €{report.ending_portfolio_value:,.2f}")
    print(f"  Win Rate       : {report.win_rate_pct:.1f}%")
    print(f"  Profit Factor  : {report.profit_factor:.2f}x")
    print(f"  Open Positions : {report.open_positions}")
    print(f"  Closed Trades  : {report.closed_trades}")

    if report.alpha_beta:
        ab = report.alpha_beta
        print(f"\n{sep}")
        print(f"  ALPHA / BETA vs {ab.benchmark_name}")
        print(f"{sep}")
        print(f"  Portfolio Return : {_fmt_pct(ab.portfolio_return_pct)}")
        print(f"  Benchmark Return : {_fmt_pct(ab.benchmark_return_pct)}")
        print(f"  Active Return    : {_fmt_pct(ab.active_return_pct)}")
        print(f"  Beta (β)         : {ab.beta:.3f}")
        print(f"  Alpha (α, ann.)  : {_fmt_pct(ab.alpha_annualized_pct)}")
        print(f"  Tracking Error   : {ab.tracking_error_ann:.2f}% (ann.)")
        print(f"  Information Ratio: {ab.information_ratio:.3f}")
        print(f"  Sharpe Ratio     : {ab.sharpe_ratio:.3f}")

    print(f"\n{sep}")
    print("  ASSET CLASS ATTRIBUTION")
    print(f"{sep}")
    print(f"  {'Asset Class':<30}  {'P&L (EUR)':>12}  {'Contribution':>13}  Positions")
    print(f"  {'-'*30}  {'-'*12}  {'-'*13}  {'-'*9}")
    for ac in report.asset_classes:
        print(
            f"  {ac.asset_class:<30}  "
            f"{_fmt_eur(ac.total_pnl_eur):>12}  "
            f"{_fmt_pct(ac.portfolio_contribution_pct, 3):>13}  "
            f"{ac.position_count:>9}"
        )

    print(f"\n{sep}")
    print("  TOP 5 CONTRIBUTORS")
    print(f"{sep}")
    for c in report.top_contributors[:5]:
        print(
            f"  {c['rank']:>2}. {c['symbol']:<12}  "
            f"{_fmt_eur(c['total_pnl_eur']):>10}  "
            f"{_fmt_pct(c['portfolio_contribution_pct'], 3):>10}  "
            f"{c['sector'][:22]}"
        )

    if report.top_detractors:
        print(f"\n{sep}")
        print("  TOP 5 DETRACTORS")
        print(f"{sep}")
        for d in report.top_detractors[:5]:
            print(
                f"  {d['rank']:>2}. {d['symbol']:<12}  "
                f"{_fmt_eur(d['total_pnl_eur']):>10}  "
                f"{_fmt_pct(d['portfolio_contribution_pct'], 3):>10}  "
                f"{d.get('exit_reason', '—')[:22]}"
            )

    if report.warnings:
        print(f"\n{sep}")
        print(f"  WARNINGS ({len(report.warnings)})")
        print(f"{sep}")
        for w in report.warnings:
            print(f"  ⚠  {w}")

    print(f"\n{SEP}\n")


# ============================================================================
# CLI ARGUMENT PARSER
# ============================================================================

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Script 22 — Performance Attribution",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python 22_performance_attribution.py --month 2026-01 --account-equity 52000
  python 22_performance_attribution.py --start-date 2025-07-01 --end-date 2026-01-31
  python 22_performance_attribution.py --month 2026-01 --benchmark ACWI.US
        """,
    )

    # Period definition
    period_group = parser.add_mutually_exclusive_group()
    period_group.add_argument(
        "--month",
        metavar="YYYY-MM",
        help="Analyse a full calendar month (sets start and end dates automatically).",
    )
    period_group.add_argument(
        "--year",
        metavar="YYYY",
        help=(
            "Year mode: compute YTD attribution AND one report per completed calendar "
            "month. For past years covers Jan-Dec. For the current year covers "
            "Jan through today, with per-month breakdowns."
        ),
    )
    period_group.add_argument(
        "--start-date",
        metavar="YYYY-MM-DD",
        help="Analysis period start date. Requires --end-date.",
    )

    parser.add_argument(
        "--end-date",
        metavar="YYYY-MM-DD",
        default=date.today().isoformat(),
        help="Analysis period end date (default: today).",
    )

    # Attribution options
    parser.add_argument(
        "--benchmark",
        default="SPY.US",
        choices=["SPY.US", "ACWI.US", "QQQ.US"],
        help="Benchmark for alpha/beta decomposition (default: SPY.US).",
    )
    parser.add_argument(
        "--account-equity",
        type=float,
        default=0.0,
        metavar="EUR",
        help="Current account equity in EUR (used for VaR scaling).",
    )
    parser.add_argument(
        "--risk-free-rate",
        type=float,
        default=0.02,
        metavar="FLOAT",
        help="Annual risk-free rate for Sharpe calculation (default: 0.02 = 2%%).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and display results without writing any output files.",
    )

    add_strategy_argument(parser)
    return parser.parse_args()


def resolve_dates(args: argparse.Namespace) -> Tuple[date, date, str]:
    """
    Resolve (start_date, end_date, period_label) from CLI arguments.

    Priority: --year > --month > --start-date/--end-date > default.
    For --year the returned range is the full YTD window; per-month breakdown
    is handled separately in run_year_mode().
    """
    today = date.today()

    if getattr(args, "year", None):
        try:
            y = int(args.year)
        except (ValueError, TypeError):
            logger.error(f"Invalid --year format: '{args.year}'. Use YYYY (e.g. 2026).")
            sys.exit(1)
        start = date(y, 1, 1)
        end   = min(date(y, 12, 31), today)
        label = str(y)

    elif args.month:
        try:
            y, m = map(int, args.month.split("-"))
            start = date(y, m, 1)
            last_day = calendar.monthrange(y, m)[1]
            end = date(y, m, last_day)
            label = args.month
        except (ValueError, AttributeError):
            logger.error(f"Invalid --month format: '{args.month}'. Use YYYY-MM.")
            sys.exit(1)

    elif args.start_date:
        try:
            start = date.fromisoformat(args.start_date)
            end   = date.fromisoformat(args.end_date)
            label = f"{start.isoformat()}_{end.isoformat()}"
        except ValueError as e:
            logger.error(f"Invalid date format: {e}")
            sys.exit(1)

    else:
        # Default: current month to date
        start = date(today.year, today.month, 1)
        end   = today
        label = f"{today.year}-{today.month:02d}"
        logger.info(f"No period specified — defaulting to current month: {label}")

    if start > end:
        logger.error(f"start_date ({start}) must be before end_date ({end}).")
        sys.exit(1)

    return start, end, label


def enumerate_months(year: int) -> List[Tuple[date, date, str, str, bool]]:
    """
    Return (start, end, label, name, is_complete) for every calendar month in
    `year` up to and including today.

    is_complete = True  — month has fully elapsed (full Jan 1 – last-day window)
    is_complete = False — current partial month (Jan 1 – today window)
    """
    today = date.today()
    month_names = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ]
    months = []
    for m in range(1, 13):
        m_start = date(year, m, 1)
        if m_start > today:
            break  # future month — skip
        last_day   = calendar.monthrange(year, m)[1]
        m_end_full = date(year, m, last_day)
        is_complete = m_end_full <= today
        m_end  = m_end_full if is_complete else today
        label  = f"{year}-{m:02d}"
        name   = f"{month_names[m - 1]} {year}"
        months.append((m_start, m_end, label, name, is_complete))
    return months


# ============================================================================
# SINGLE-PERIOD ATTRIBUTION (extracted from main for reuse in year mode)
# ============================================================================

def run_single_period_attribution(
    start_date:       date,
    end_date:         date,
    period_label:     str,
    all_trades:       List[Dict],
    open_positions:   Dict,
    company_info:     Dict,
    account_equity:   float,
    benchmark:        str,
    risk_free_rate:   float,
) -> AttributionReport:
    """
    Core attribution engine for a single period.

    Separated from main() so that year mode can call it repeatedly for each
    month and for the full YTD window without duplicating logic.

    Returns a fully populated AttributionReport.
    """
    warnings_local: List[str] = []

    period_trades = load_trade_ledger(start_date, end_date)

    # ── Starting portfolio value ──────────────────────────────────────────────
    cost_bases = [
        (pos.get("entry_price", 0) or 0) * (pos.get("shares", 0) or 0)
        for pos in open_positions.values()
    ]
    open_cost_basis   = sum(cost_bases)
    closed_cost_bases = [
        t.get("fill_price", 0) * t.get("shares", 0)
        for t in period_trades if t.get("action") == "BUY"
    ]
    closed_basis_sum = sum(closed_cost_bases)

    starting_portfolio_value = account_equity if account_equity > 0 else max(
        open_cost_basis + closed_basis_sum, 1.0
    )

    # ── Position attribution ──────────────────────────────────────────────────
    positions = compute_position_attribution(
        period_trades            = period_trades,
        all_trades               = all_trades,
        open_positions           = open_positions,
        company_info             = company_info,
        start_date               = start_date,
        end_date                 = end_date,
        starting_portfolio_value = starting_portfolio_value,
    )

    if not positions and not period_trades:
        warnings_local.append("No trade data found for this period.")

    # ── Asset class / sector aggregation ─────────────────────────────────────
    asset_classes = aggregate_by_asset_class(positions)
    sectors       = aggregate_by_sector(positions)

    # ── Alpha / Beta ──────────────────────────────────────────────────────────
    alpha_beta = None
    if positions:
        portfolio_returns = reconstruct_portfolio_returns(
            positions      = positions,
            open_positions = open_positions,
            all_trades     = all_trades,
            start_date     = start_date,
            end_date       = end_date,
        )
        if portfolio_returns is not None and len(portfolio_returns) >= 10:
            alpha_beta = compute_alpha_beta(
                portfolio_returns = portfolio_returns,
                benchmark_symbol  = benchmark,
                start_date        = start_date,
                end_date          = end_date,
                risk_free_rate    = risk_free_rate,
            )
        else:
            warnings_local.append(
                "Alpha/beta skipped: insufficient daily price data for this period."
            )

    # ── Assemble ──────────────────────────────────────────────────────────────
    report = assemble_report(
        positions              = positions,
        asset_classes          = asset_classes,
        sectors                = sectors,
        alpha_beta             = alpha_beta,
        period_trades          = period_trades,
        open_positions         = open_positions,
        start_date             = start_date,
        end_date               = end_date,
        period_label           = period_label,
        account_equity_eur     = account_equity,
        warnings               = warnings_local,
    )
    return report


# ============================================================================
# YEAR MODE — MONTHLY BREAKDOWN BUILDER
# ============================================================================

def build_monthly_breakdown(
    report:       AttributionReport,
    month_label:  str,
    month_name:   str,
    month_start:  date,
    month_end:    date,
    is_complete:  bool,
) -> MonthlyBreakdown:
    """Distil an AttributionReport into a MonthlyBreakdown summary row."""
    ab = report.alpha_beta

    top_c_sym = report.top_contributors[0]["symbol"] if report.top_contributors else "—"
    top_c_pnl = report.top_contributors[0]["total_pnl_eur"] if report.top_contributors else 0.0
    top_d_sym = report.top_detractors[0]["symbol"]  if report.top_detractors  else "—"
    top_d_pnl = report.top_detractors[0]["total_pnl_eur"]  if report.top_detractors  else 0.0

    return MonthlyBreakdown(
        month_label             = month_label,
        month_name              = month_name,
        month_start             = month_start.isoformat(),
        month_end               = month_end.isoformat(),
        is_complete             = is_complete,
        return_pct              = report.total_return_pct,
        pnl_eur                 = report.total_pnl_eur,
        win_rate_pct            = report.win_rate_pct,
        profit_factor           = report.profit_factor,
        open_positions          = report.open_positions,
        closed_trades           = report.closed_trades,
        alpha_annualized_pct    = ab.alpha_annualized_pct    if ab else 0.0,
        benchmark_return_pct    = ab.benchmark_return_pct    if ab else 0.0,
        active_return_pct       = ab.active_return_pct       if ab else 0.0,
        sharpe_ratio            = ab.sharpe_ratio            if ab else 0.0,
        information_ratio       = ab.information_ratio       if ab else 0.0,
        top_contributor_symbol  = top_c_sym,
        top_contributor_pnl     = top_c_pnl,
        top_detractor_symbol    = top_d_sym,
        top_detractor_pnl       = top_d_pnl,
        report                  = report,
    )


# ============================================================================
# YEAR MODE — EXPORTS
# ============================================================================

def export_year_json(
    ytd_report:       AttributionReport,
    monthly_reports:  List[MonthlyBreakdown],
    year:             int,
    dry_run:          bool = False,
) -> Optional[Path]:
    """
    Export a consolidated year JSON containing:
      - YTD attribution (full AttributionReport payload)
      - Monthly summary table (MonthlyBreakdown list)
      - Individual monthly attribution payloads
    """
    PERF_DIR.mkdir(parents=True, exist_ok=True)
    out_path = PERF_DIR / f"{year}_year_attribution.json"

    def _report_to_dict(r: AttributionReport) -> Dict:
        return {
            "period_start":             r.period_start,
            "period_end":               r.period_end,
            "period_label":             r.period_label,
            "generated_at":             r.generated_at,
            "total_return_pct":         r.total_return_pct,
            "total_pnl_eur":            r.total_pnl_eur,
            "starting_portfolio_value": r.starting_portfolio_value,
            "ending_portfolio_value":   r.ending_portfolio_value,
            "win_rate_pct":             r.win_rate_pct,
            "profit_factor":            r.profit_factor,
            "open_positions":           r.open_positions,
            "closed_trades":            r.closed_trades,
            "asset_class_attribution":  [asdict(ac) for ac in r.asset_classes],
            "sector_attribution":       [asdict(s)  for s  in r.sectors],
            "position_attribution":     [asdict(p)  for p  in r.positions],
            "alpha_beta":               asdict(r.alpha_beta) if r.alpha_beta else None,
            "top_contributors":         r.top_contributors,
            "top_detractors":           r.top_detractors,
            "warnings":                 r.warnings,
        }

    monthly_summary = []
    for mb in monthly_reports:
        row = {
            "month_label":            mb.month_label,
            "month_name":             mb.month_name,
            "month_start":            mb.month_start,
            "month_end":              mb.month_end,
            "is_complete":            mb.is_complete,
            "return_pct":             mb.return_pct,
            "pnl_eur":                mb.pnl_eur,
            "win_rate_pct":           mb.win_rate_pct,
            "profit_factor":          mb.profit_factor,
            "open_positions":         mb.open_positions,
            "closed_trades":          mb.closed_trades,
            "alpha_annualized_pct":   mb.alpha_annualized_pct,
            "benchmark_return_pct":   mb.benchmark_return_pct,
            "active_return_pct":      mb.active_return_pct,
            "sharpe_ratio":           mb.sharpe_ratio,
            "information_ratio":      mb.information_ratio,
            "top_contributor":        mb.top_contributor_symbol,
            "top_contributor_pnl":    mb.top_contributor_pnl,
            "top_detractor":          mb.top_detractor_symbol,
            "top_detractor_pnl":      mb.top_detractor_pnl,
            "full_report":            _report_to_dict(mb.report) if mb.report else None,
        }
        monthly_summary.append(row)

    payload = {
        "year":             year,
        "generated_at":     datetime.now().isoformat(),
        "ytd_attribution":  _report_to_dict(ytd_report),
        "monthly_summary":  monthly_summary,
    }

    if dry_run:
        logger.info(f"  [DRY-RUN] Would write year JSON → {out_path}")
        return out_path

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    logger.info(f"  Year JSON → {out_path}")
    return out_path


def export_year_csv(
    ytd_report:       AttributionReport,
    monthly_reports:  List[MonthlyBreakdown],
    year:             int,
    dry_run:          bool = False,
) -> Optional[Path]:
    """
    Export two CSV files:
      1. {year}_monthly_summary.csv — one row per month (heatmap-ready)
      2. {year}_ytd_positions.csv   — full position-level YTD attribution
    """
    import csv as csv_mod
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # ── File 1: Monthly summary ───────────────────────────────────────────────
    summary_path = REPORTS_DIR / f"{year}_monthly_summary.csv"
    summary_rows = []
    for mb in monthly_reports:
        summary_rows.append({
            "Month":              mb.month_name,
            "Label":              mb.month_label,
            "Complete":           "Yes" if mb.is_complete else "Partial",
            "Return (%)":         round(mb.return_pct, 3),
            "P&L (EUR)":          round(mb.pnl_eur, 2),
            "Win Rate (%)":       round(mb.win_rate_pct, 1),
            "Profit Factor":      round(mb.profit_factor, 2) if mb.profit_factor != float("inf") else "inf",
            "Open Positions":     mb.open_positions,
            "Closed Trades":      mb.closed_trades,
            "Alpha Ann. (%)":     round(mb.alpha_annualized_pct, 3),
            "Benchmark Ret. (%)": round(mb.benchmark_return_pct, 3),
            "Active Ret. (%)":    round(mb.active_return_pct, 3),
            "Sharpe":             round(mb.sharpe_ratio, 3),
            "Info. Ratio":        round(mb.information_ratio, 3),
            "Best Position":      mb.top_contributor_symbol,
            "Best P&L (EUR)":     round(mb.top_contributor_pnl, 2),
            "Worst Position":     mb.top_detractor_symbol,
            "Worst P&L (EUR)":    round(mb.top_detractor_pnl, 2),
        })

    if not dry_run:
        with open(summary_path, "w", newline="", encoding="utf-8") as f:
            if summary_rows:
                writer = csv_mod.DictWriter(f, fieldnames=summary_rows[0].keys())
                writer.writeheader()
                writer.writerows(summary_rows)
        logger.info(f"  Year CSV summary → {summary_path}")
    else:
        logger.info(f"  [DRY-RUN] Would write → {summary_path}")

    # ── File 2: YTD positions ─────────────────────────────────────────────────
    ytd_path  = REPORTS_DIR / f"{year}_ytd_positions.csv"
    pos_rows  = []
    for p in ytd_report.positions:
        pos_rows.append({
            "Symbol":              p.symbol,
            "Name":                p.name,
            "Asset Class":         p.asset_class,
            "Sector":              p.sector,
            "Status":              "OPEN" if p.is_open else "CLOSED",
            "Entry Date":          p.entry_date,
            "Exit Date":           p.exit_date if not p.is_open else "",
            "Entry Price (EUR)":   round(p.entry_price, 4),
            "Exit Price (EUR)":    round(p.exit_price, 4) if p.exit_price else "",
            "Shares":              p.shares,
            "Realized P&L (EUR)":  round(p.realized_pnl_eur, 2),
            "Unrealized P&L (EUR)": round(p.unrealized_pnl_eur, 2),
            "Total P&L (EUR)":     round(p.total_pnl_eur, 2),
            "Position Return (%)": round(p.position_return_pct, 2),
            "Portfolio Contribution (%)": round(p.portfolio_contribution_pct, 3),
            "Exit Reason":         p.exit_reason,
        })

    if not dry_run:
        with open(ytd_path, "w", newline="", encoding="utf-8") as f:
            if pos_rows:
                writer = csv_mod.DictWriter(f, fieldnames=pos_rows[0].keys())
                writer.writeheader()
                writer.writerows(pos_rows)
        logger.info(f"  Year CSV YTD positions → {ytd_path}")
    else:
        logger.info(f"  [DRY-RUN] Would write → {ytd_path}")

    return summary_path



# ============================================================================
# EOM PRICE HELPERS — used by year mode to compute correct monthly returns
# ============================================================================

def get_eom_price(symbol: str, year: int, month: int) -> Optional[float]:
    """
    Return the closing price on the last available trading day of the given
    calendar month, sourced from data_cache/consolidated/{symbol}.parquet.

    Returns None if no parquet data is available for that month.

    This is the fix for the "identical months" bug: previously all months
    used the live current price from portfolio_state.json.  Now each month
    gets its own historically correct end-of-month price.
    """
    series = load_price_series(symbol)
    if series is None or len(series) == 0:
        return None

    import calendar as _cal
    last_day  = _cal.monthrange(year, month)[1]
    mo_start  = pd.Timestamp(date(year, month, 1))
    mo_end    = pd.Timestamp(date(year, month, last_day))

    # Slice to the month window
    month_data = series.loc[mo_start:mo_end]
    if len(month_data) == 0:
        return None

    return float(month_data.iloc[-1])


def build_position_monthly_matrix(
    positions:   List["PositionAttribution"],
    all_trades:  List[Dict],
    year:        int,
) -> Dict[str, Dict[int, Dict]]:
    """
    For each position and each calendar month in `year`, compute:
        eom_price  — closing price on last trading day of that month
        mo_ret_pct — (eom_price - prev_ref) / prev_ref * 100
                     prev_ref = buy price for the first month held,
                                prior month's eom_price for subsequent months
        ytd_ret_pct — (eom_price - buy_price) / buy_price * 100
        held        — True if position was open at any point in that month

    Returns:
        {symbol: {month_num (1–12): {"eom_price", "mo_ret_pct",
                                      "ytd_ret_pct", "held"}}}

    A month is considered "held" if:
        entry_date <= last day of month  AND
        (exit_date >= first day of month  OR  position is still open)
    """
    today    = date.today()
    result: Dict[str, Dict[int, Dict]] = {}

    # Build entry/exit date map from all_trades for accuracy
    # (portfolio_state only has current open positions)
    buy_map:  Dict[str, date] = {}
    sell_map: Dict[str, date] = {}
    for t in sorted(all_trades, key=lambda x: x.get("execution_date", "")):
        sym  = t.get("symbol", "")
        tdate_str = t.get("execution_date", "")
        if not sym or not tdate_str:
            continue
        try:
            tdate = date.fromisoformat(tdate_str)
        except ValueError:
            continue
        if t.get("action") == "BUY":
            if sym not in buy_map:           # first buy in history
                buy_map[sym] = tdate
        elif t.get("action") == "SELL":
            sell_map[sym] = tdate            # last sell date

    import calendar as _cal

    for pos in positions:
        sym        = pos.symbol
        entry_date = date.fromisoformat(pos.entry_date) if pos.entry_date else buy_map.get(sym)
        exit_date  = date.fromisoformat(pos.exit_date)  if pos.exit_date and not pos.is_open else None
        buy_price  = pos.entry_price

        if not entry_date or buy_price <= 0:
            continue

        months: Dict[int, Dict] = {}
        prev_ref = buy_price   # reference price for month-over-month calculation

        for m in range(1, 13):
            m_last_day = _cal.monthrange(year, m)[1]
            m_first    = date(year, m, 1)
            m_last     = date(year, m, m_last_day)

            # ── Is the position held this month? ─────────────────────────────
            position_started = entry_date <= m_last
            position_active  = (exit_date is None) or (exit_date >= m_first)
            held = position_started and position_active

            # ── Future month? ────────────────────────────────────────────────
            is_future = m_first > today

            if not held or is_future:
                months[m] = {"eom_price": None, "mo_ret_pct": None,
                              "ytd_ret_pct": None, "held": held and not is_future}
                continue

            # ── EoM price ────────────────────────────────────────────────────
            # For the current partial month use today's price (last available)
            eom_price = get_eom_price(sym, year, m)
            if eom_price is None:
                months[m] = {"eom_price": None, "mo_ret_pct": None,
                              "ytd_ret_pct": None, "held": True}
                prev_ref = prev_ref   # keep prev_ref unchanged
                continue

            # ── Returns ──────────────────────────────────────────────────────
            # Month return: use buy_price as baseline if this is the first
            # month the position was held (entry in this month or earlier in year)
            if entry_date >= m_first:
                # Entered mid-month: baseline is the actual buy price
                ref = buy_price
            else:
                ref = prev_ref

            mo_ret  = (eom_price - ref)       / ref       * 100 if ref  > 0 else 0.0
            ytd_ret = (eom_price - buy_price) / buy_price * 100 if buy_price > 0 else 0.0

            months[m] = {
                "eom_price":   round(eom_price, 4),
                "mo_ret_pct":  round(mo_ret, 2),
                "ytd_ret_pct": round(ytd_ret, 2),
                "held":        True,
            }
            prev_ref = eom_price   # this month's close becomes next month's baseline

        result[sym] = months

    return result


# ============================================================================
# YEAR PDF — TWO-TABLE LAYOUT (landscape)
# ============================================================================

def _val_color(val: Optional[float], neutral_color=None) -> object:
    """Return C_SUCCESS / C_DANGER / C_HOLD based on sign of val."""
    if val is None:
        return C_HOLD
    if val > 0:
        return C_SUCCESS
    if val < 0:
        return C_DANGER
    return neutral_color or C_HOLD


def generate_year_pdf(
    ytd_report:       "AttributionReport",
    monthly_reports:  "List[MonthlyBreakdown]",
    year:             int,
    benchmark_name:   str,
    all_positions:    "List[PositionAttribution]",
    all_trades:       List[Dict],
    dry_run:          bool = False,
) -> Optional[Path]:
    """
    Generate the full-year attribution PDF in A4 LANDSCAPE format.

    Structure
    ─────────
    Page 1   YTD Executive Summary banner + benchmark comparison table
    Page 1   Monthly aggregate heatmap (return / P&L / alpha / IR per month)
    Page 2   TABLE 1 — Position Summary
               Identity (symbol, name, buy date, shares, buy price)
               Current month block (current price, month return, prior ref)
               YTD block (P&L €, YTD return %, status badge)
    Page 3   TABLE 2 — Monthly Return Matrix (compact)
               One row per symbol, one column-pair per month (Mo. + YTD)
               12 months across one landscape page at 7pt font
               Dash (—) = not held / future
    Page N+  Per-month detail sections (one per completed month)

    Bug fix
    ───────
    EoM prices now come from get_eom_price() which reads the last available
    closing price from data_cache/consolidated/{symbol}.parquet for each
    month — NOT the current live price.  This ensures Jan and Feb (and every
    other month) show different, historically correct performance figures.
    """
    if not REPORTLAB_AVAILABLE:
        logger.warning("ReportLab not installed — skipping PDF generation.")
        return None

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"attribution_{year}_full_year.pdf"

    if dry_run:
        logger.info(f"  [DRY-RUN] Would write year PDF → {out_path}")
        return out_path

    # ── Page geometry (A4 landscape) ─────────────────────────────────────────
    from reportlab.lib.pagesizes import landscape
    PAGE_SIZE = landscape(A4)          # 841.9 × 595.3 pt
    MARGIN_H  = 15 * mm
    MARGIN_V  = 18 * mm
    CONTENT_W = PAGE_SIZE[0] - 2 * MARGIN_H   # ≈ 811 pt
    CONTENT_H = PAGE_SIZE[1] - 2 * MARGIN_V   # ≈ 559 pt

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize   = PAGE_SIZE,
        leftMargin = MARGIN_H, rightMargin  = MARGIN_H,
        topMargin  = MARGIN_V, bottomMargin = MARGIN_V,
        title      = f"Performance Attribution {year} — Full Year",
    )

    # ── Styles ────────────────────────────────────────────────────────────────
    H1    = ParagraphStyle("H1Y",   fontSize=15, fontName="Helvetica-Bold",
                            textColor=C_NAVY, spaceBefore=2, spaceAfter=3)
    H2    = ParagraphStyle("H2Y",   fontSize=10, fontName="Helvetica-Bold",
                            textColor=C_ACCENT, spaceBefore=8, spaceAfter=3)
    H3    = ParagraphStyle("H3Y",   fontSize=9,  fontName="Helvetica-Bold",
                            textColor=C_NAVY, spaceBefore=6, spaceAfter=2)
    SMALL = ParagraphStyle("SmY",   fontSize=7,  fontName="Helvetica",
                            textColor=C_HOLD, spaceAfter=1)
    TINY  = ParagraphStyle("TiY",   fontSize=6.5,fontName="Helvetica",
                            textColor=C_HOLD, spaceAfter=1)

    story = []

    # ── HEADER ───────────────────────────────────────────────────────────────
    story.append(Paragraph(f"Performance Attribution — Full Year {year}", H1))
    story.append(Paragraph(
        f"YTD: {ytd_report.period_start}  →  {ytd_report.period_end}  |  "
        f"Benchmark: {benchmark_name}  |  "
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        SMALL,
    ))
    story.append(HRFlowable(width=CONTENT_W, thickness=1.5, color=C_NAVY))
    story.append(Spacer(1, 3 * mm))

    # ── YTD EXECUTIVE SUMMARY BANNER ─────────────────────────────────────────
    ab = ytd_report.alpha_beta
    rc = C_SUCCESS if ytd_report.total_return_pct >= 0 else C_DANGER

    ytd_data = [
        ["YTD Return", "Total P&L (EUR)", "Win Rate", "Profit Factor",
         "Open Positions", "Closed Trades", "Alpha (ann.)", "Sharpe Ratio"],
        [
            _fmt_pct(ytd_report.total_return_pct),
            _fmt_eur(ytd_report.total_pnl_eur),
            f"{ytd_report.win_rate_pct:.1f}%",
            f"{ytd_report.profit_factor:.2f}x" if ytd_report.profit_factor != float("inf") else "inf",
            str(ytd_report.open_positions),
            str(ytd_report.closed_trades),
            _fmt_pct(ab.alpha_annualized_pct) if ab else "N/A",
            f"{ab.sharpe_ratio:.3f}"           if ab else "N/A",
        ],
    ]
    ytd_tbl = Table(ytd_data, colWidths=[CONTENT_W / 8] * 8)
    ytd_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), C_NAVY),
        ("TEXTCOLOR",     (0, 0), (-1, 0), C_WHITE),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0), 7),
        ("FONTSIZE",      (0, 1), (-1, 1), 11),
        ("FONTNAME",      (0, 1), (-1, 1), "Helvetica-Bold"),
        ("TEXTCOLOR",     (0, 1), (1, 1), rc),
        ("TEXTCOLOR",     (2, 1), (5, 1), C_ACCENT),
        ("TEXTCOLOR",     (6, 1), (-1, 1), C_SUCCESS if (ab and ab.alpha_annualized_pct >= 0) else C_DANGER),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("GRID",          (0, 0), (-1, -1), 0.3, C_HOLD),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(ytd_tbl)
    story.append(Spacer(1, 2 * mm))

    # Benchmark comparison row
    if ab:
        bench_data = [
            ["", "Portfolio", f"Benchmark ({benchmark_name})",
             "Active Return", "Tracking Error", "Info. Ratio", "Beta (β)", "R²"],
            [
                "Period Return",
                _fmt_pct(ab.portfolio_return_pct),
                _fmt_pct(ab.benchmark_return_pct),
                _fmt_pct(ab.active_return_pct),
                f"{ab.tracking_error_ann:.2f}%",
                f"{ab.information_ratio:.3f}",
                f"{ab.beta:.3f}",
                f"{ab.r_squared:.3f}",
            ],
        ]
        bw = [CONTENT_W * 0.14] + [CONTENT_W * 0.123] * 7
        btbl = Table(bench_data, colWidths=bw)
        bs = _table_style_base()
        ar_c = C_SUCCESS if ab.active_return_pct >= 0 else C_DANGER
        bs.extend([
            ("TEXTCOLOR", (3, 1), (3, 1), ar_c),
            ("FONTNAME",  (3, 1), (3, 1), "Helvetica-Bold"),
        ])
        btbl.setStyle(TableStyle(bs))
        story.append(btbl)

    story.append(Spacer(1, 5 * mm))

    # ── MONTHLY AGGREGATE HEATMAP ─────────────────────────────────────────────
    story.append(Paragraph("Monthly Aggregate Performance", H2))

    MONTH_ABBR = ["Jan","Feb","Mar","Apr","May","Jun",
                  "Jul","Aug","Sep","Oct","Nov","Dec"]
    heat_hdr = ["Month", "Return (%)", "P&L (EUR)", "Win Rate",
                "Profit Factor", "Alpha (ann.)", "Active Ret.", "Sharpe",
                "Info. Ratio", "Best Position", "Worst Position"]
    heat_rows = [heat_hdr]

    for mb in monthly_reports:
        pf_s = f"{mb.profit_factor:.2f}x" if mb.profit_factor != float("inf") else "inf"
        partial = " *" if not mb.is_complete else ""
        heat_rows.append([
            MONTH_ABBR[int(mb.month_label.split("-")[1]) - 1] + partial,
            _fmt_pct(mb.return_pct),
            _fmt_eur(mb.pnl_eur),
            f"{mb.win_rate_pct:.0f}%",
            pf_s,
            _fmt_pct(mb.alpha_annualized_pct) if mb.alpha_annualized_pct else "N/A",
            _fmt_pct(mb.active_return_pct)    if mb.active_return_pct    else "N/A",
            f"{mb.sharpe_ratio:.2f}"          if mb.sharpe_ratio         else "N/A",
            f"{mb.information_ratio:.2f}"     if mb.information_ratio    else "N/A",
            mb.top_contributor_symbol,
            mb.top_detractor_symbol,
        ])

    ytd_pf = f"{ytd_report.profit_factor:.2f}x" if ytd_report.profit_factor != float("inf") else "inf"
    heat_rows.append([
        "YTD",
        _fmt_pct(ytd_report.total_return_pct),
        _fmt_eur(ytd_report.total_pnl_eur),
        f"{ytd_report.win_rate_pct:.0f}%",
        ytd_pf,
        _fmt_pct(ab.alpha_annualized_pct) if ab else "N/A",
        _fmt_pct(ab.active_return_pct)    if ab else "N/A",
        f"{ab.sharpe_ratio:.2f}"          if ab else "N/A",
        f"{ab.information_ratio:.2f}"     if ab else "N/A",
        "—", "—",
    ])

    hw = [CONTENT_W * 0.055, CONTENT_W * 0.075, CONTENT_W * 0.09,
          CONTENT_W * 0.06,  CONTENT_W * 0.07,  CONTENT_W * 0.08,
          CONTENT_W * 0.08,  CONTENT_W * 0.065, CONTENT_W * 0.065,
          CONTENT_W * 0.13,  CONTENT_W * 0.13]
    htbl = Table(heat_rows, colWidths=hw, repeatRows=1)
    hs   = _table_style_base()
    for ri, mb in enumerate(monthly_reports, start=1):
        rc2 = C_SUCCESS if mb.return_pct >= 0 else C_DANGER
        hs.extend([
            ("TEXTCOLOR", (1, ri), (2, ri), rc2),
            ("FONTNAME",  (1, ri), (2, ri), "Helvetica-Bold"),
        ])
    # YTD totals row
    ytd_ri = len(monthly_reports) + 1
    ytd_rc = C_SUCCESS if ytd_report.total_return_pct >= 0 else C_DANGER
    hs.extend([
        ("BACKGROUND", (0, ytd_ri), (-1, ytd_ri), C_NAVY),
        ("TEXTCOLOR",  (0, ytd_ri), (-1, ytd_ri), C_WHITE),
        ("FONTNAME",   (0, ytd_ri), (-1, ytd_ri), "Helvetica-Bold"),
        ("TEXTCOLOR",  (1, ytd_ri), (2, ytd_ri),  ytd_rc),
    ])
    htbl.setStyle(TableStyle(hs))
    story.append(htbl)
    if any(not mb.is_complete for mb in monthly_reports):
        story.append(Paragraph("* Partial month (data up to today).", TINY))
    story.append(Spacer(1, 3 * mm))

    # ═══════════════════════════════════════════════════════════════════════
    # TABLE 1 — POSITION SUMMARY
    # ═══════════════════════════════════════════════════════════════════════
    story.append(PageBreak())
    story.append(Paragraph("Position Summary", H2))
    story.append(Paragraph(
        "Identity · current month performance · YTD summary",
        SMALL,
    ))
    story.append(Spacer(1, 2 * mm))

    # Determine current month label for the header
    today = date.today()
    cur_month_label = today.strftime("%B %Y")
    cur_year_m      = (today.year, today.month)

    # Build group header rows (2-level)
    # Columns: Symbol | Name | Buy Date | Shares | Buy Price |
    #          [Current Month: Current Price | Mo. Return | vs Prior] |
    #          [YTD: P&L (EUR) | YTD Ret. | Status]
    t1_grp = [
        ("Position Identity",   5),
        (f"{cur_month_label}",  3),
        ("YTD Summary",         3),
    ]

    # Group header row
    t1_grp_row = []
    for label, span in t1_grp:
        t1_grp_row.append(label)
        t1_grp_row.extend([""] * (span - 1))

    t1_sub_row = [
        "Symbol", "Name", "Buy Date", "Shares", "Buy Price",
        "Current Price", "Month Ret.", "vs Prior Close",
        "P&L (EUR)", "YTD Ret.", "Status",
    ]

    t1_data = [t1_grp_row, t1_sub_row]

    # Populate rows — use positions from ytd_report sorted by total P&L desc
    sorted_pos = sorted(all_positions, key=lambda p: p.total_pnl_eur, reverse=True)

    for p in sorted_pos:
        # Current price
        cur_price = p.exit_price if p.exit_price and p.exit_price > 0 else 0.0
        if cur_price == 0.0:
            ps = load_price_series(p.symbol)
            if ps is not None and len(ps) > 0:
                cur_price = float(ps.iloc[-1])

        # Prior EoM price (end of last completed month)
        prev_m = today.month - 1 if today.month > 1 else 12
        prev_y = today.year      if today.month > 1 else today.year - 1
        prior_close = get_eom_price(p.symbol, prev_y, prev_m)

        # Month return: vs prior close or buy price if bought this month
        entry_d = date.fromisoformat(p.entry_date) if p.entry_date else None
        is_new_this_month = (
            entry_d and entry_d.year == cur_year_m[0] and entry_d.month == cur_year_m[1]
        )
        ref_price = p.entry_price if (is_new_this_month or prior_close is None) else prior_close
        mo_ret = (cur_price - ref_price) / ref_price * 100 if ref_price > 0 and cur_price > 0 else 0.0

        # vs prior label
        if is_new_this_month or prior_close is None:
            vs_label = f"vs {_fmt_price(p.entry_price)} (entry)"
        else:
            vs_label = f"vs {_fmt_price(prior_close)}"

        t1_data.append([
            p.symbol,
            (p.name or p.symbol)[:22],
            p.entry_date or "—",
            str(p.shares) if p.shares else "—",
            _fmt_price(p.entry_price) if p.entry_price else "—",
            _fmt_price(cur_price)     if cur_price      else "—",
            _fmt_pct(mo_ret),
            vs_label,
            _fmt_eur(p.total_pnl_eur),
            _fmt_pct(p.position_return_pct),
            "OPEN" if p.is_open else "CLOSED",
        ])

    # Column widths for Table 1 (landscape, CONTENT_W ≈ 811pt)
    t1_w = [
        CONTENT_W * 0.100,   # Symbol
        CONTENT_W * 0.130,   # Name
        CONTENT_W * 0.075,   # Buy Date
        CONTENT_W * 0.045,   # Shares
        CONTENT_W * 0.075,   # Buy Price
        CONTENT_W * 0.085,   # Current Price
        CONTENT_W * 0.075,   # Month Ret.
        CONTENT_W * 0.115,   # vs Prior Close
        CONTENT_W * 0.090,   # P&L EUR
        CONTENT_W * 0.075,   # YTD Ret.
        CONTENT_W * 0.060,   # Status
    ]

    t1_tbl = Table(t1_data, colWidths=t1_w, repeatRows=2)
    t1_s   = [
        # Group header row (row 0)
        ("BACKGROUND",    (0, 0), (4, 0), colors.HexColor("#111d30")),  # Identity dark
        ("BACKGROUND",    (5, 0), (7, 0), colors.HexColor("#1a4f7a")),  # Current month mid-blue
        ("BACKGROUND",    (8, 0), (10, 0), C_ACCENT),                  # YTD blue
        ("TEXTCOLOR",     (0, 0), (-1, 0), C_WHITE),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0), 7.5),
        ("ALIGN",         (5, 0), (-1, 0), "CENTER"),
        ("ALIGN",         (0, 0), (4, 0), "LEFT"),
        # Sub-header row (row 1)
        ("BACKGROUND",    (0, 1), (-1, 1), colors.HexColor("#f0f3f8")),
        ("TEXTCOLOR",     (0, 1), (-1, 1), C_HOLD),
        ("FONTNAME",      (0, 1), (-1, 1), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 1), (-1, 1), 7),
        ("ALIGN",         (0, 1), (4, 1), "LEFT"),
        ("ALIGN",         (5, 1), (-1, 1), "RIGHT"),
        ("LINEBELOW",     (0, 1), (-1, 1), 1.5, C_NAVY),
        # Data rows
        ("FONTSIZE",      (0, 2), (-1, -1), 8),
        ("ALIGN",         (0, 2), (4, -1), "LEFT"),
        ("ALIGN",         (5, 2), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS",(0, 2), (-1, -1), [colors.HexColor("#F8F9FA"), C_WHITE]),
        ("GRID",          (0, 0), (-1, -1), 0.3, C_HOLD),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
        # Vertical dividers between groups
        ("LINEAFTER",     (4, 0), (4, -1), 1.2, C_HOLD),
        ("LINEAFTER",     (7, 0), (7, -1), 1.2, C_ACCENT),
        # YTD background tint
        ("BACKGROUND",    (8, 2), (10, -1), colors.HexColor("#f5f9ff")),
    ]

    # Colour month return and YTD columns per row
    for ri, p in enumerate(sorted_pos, start=2):
        mo_ret_val  = 0.0
        ytd_ret_val = p.position_return_pct
        pnl_val     = p.total_pnl_eur

        # Recalculate mo_ret_val for coloring
        cur_price2  = p.exit_price if p.exit_price and p.exit_price > 0 else 0.0
        if cur_price2 == 0.0:
            ps2 = load_price_series(p.symbol)
            if ps2 is not None and len(ps2) > 0:
                cur_price2 = float(ps2.iloc[-1])
        prior_close2 = get_eom_price(p.symbol,
                                     today.year if today.month > 1 else today.year - 1,
                                     today.month - 1 if today.month > 1 else 12)
        entry_d2 = date.fromisoformat(p.entry_date) if p.entry_date else None
        is_new2  = (entry_d2 and entry_d2.year == cur_year_m[0]
                    and entry_d2.month == cur_year_m[1])
        ref2 = p.entry_price if (is_new2 or prior_close2 is None) else prior_close2
        if ref2 and cur_price2:
            mo_ret_val = (cur_price2 - ref2) / ref2 * 100

        mo_c  = C_SUCCESS if mo_ret_val  > 0 else (C_DANGER if mo_ret_val  < 0 else C_HOLD)
        ytd_c = C_SUCCESS if ytd_ret_val > 0 else (C_DANGER if ytd_ret_val < 0 else C_HOLD)
        pnl_c = C_SUCCESS if pnl_val     > 0 else (C_DANGER if pnl_val     < 0 else C_HOLD)

        t1_s.extend([
            ("TEXTCOLOR", (6, ri), (6, ri), mo_c),
            ("FONTNAME",  (6, ri), (6, ri), "Helvetica-Bold"),
            ("TEXTCOLOR", (8, ri), (9, ri), pnl_c),
            ("FONTNAME",  (8, ri), (9, ri), "Helvetica-Bold"),
        ])

    t1_tbl.setStyle(TableStyle(t1_s))
    story.append(t1_tbl)
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph(
        "Current Price = last available close from data_cache/consolidated/{symbol}.parquet  |  "
        "Month Ret. = (Current Price − Prior EoM Close or Buy Price) / Prior  |  "
        "YTD Ret. = (Current Price − Buy Price) / Buy Price",
        TINY,
    ))

    # ═══════════════════════════════════════════════════════════════════════
    # TABLE 2 — MONTHLY RETURN MATRIX (compact, 12 months)
    # ═══════════════════════════════════════════════════════════════════════
    story.append(PageBreak())
    story.append(Paragraph("Monthly Return Matrix", H2))
    story.append(Paragraph(
        "Mo. = month return vs prior EoM close (or buy price for first month held)  |  "
        "YTD = cumulative return from buy price  |  — = not held or future month",
        SMALL,
    ))
    story.append(Spacer(1, 2 * mm))

    logger.info("  Building position monthly price matrix…")
    price_matrix = build_position_monthly_matrix(all_positions, all_trades, year)

    # ── Column widths: symbol col + 2 cols × 12 months ──────────────────────
    SYM_W  = CONTENT_W * 0.095         # symbol column
    MO_COL = (CONTENT_W - SYM_W) / 24  # each of 24 month sub-columns

    # Group header row: Symbol | Jan | Feb | … | Dec
    grp_row = ["Symbol"]
    for m in range(1, 13):
        grp_row.append(MONTH_ABBR[m - 1])
        grp_row.append("")   # second sub-col (empty — merged visually by spanning)

    # Sub-header row
    sub_row = ["Symbol"]
    for _ in range(12):
        sub_row.extend(["Mo.", "YTD"])

    t2_data = [grp_row, sub_row]

    # Data rows
    for p in sorted_pos:
        row = [p.symbol]
        months_data = price_matrix.get(p.symbol, {})
        for m in range(1, 13):
            md = months_data.get(m, {})
            held      = md.get("held", False)
            mo_ret    = md.get("mo_ret_pct")
            ytd_ret   = md.get("ytd_ret_pct")
            is_future = date(year, m, 1) > today

            if not held or is_future or mo_ret is None:
                row.extend(["—", "—"])
            else:
                sign_mo  = "+" if mo_ret  >= 0 else ""
                sign_ytd = "+" if ytd_ret >= 0 else ""
                row.extend([
                    f"{sign_mo}{mo_ret:.1f}%",
                    f"{sign_ytd}{ytd_ret:.1f}%",
                ])
        t2_data.append(row)

    # Column widths list
    t2_w = [SYM_W] + [MO_COL] * 24

    t2_tbl = Table(t2_data, colWidths=t2_w, repeatRows=2)

    # Base style
    C_DARK_NAVY = colors.HexColor("#111d30")
    C_PARTIAL   = colors.HexColor("#1a4f7a")
    C_FUTURE    = colors.HexColor("#2a2a2a")
    C_GREY_SUBH = colors.HexColor("#f0f3f8")

    t2_s = [
        # ── Group header row ──
        ("BACKGROUND",   (0, 0), (0, 0), C_DARK_NAVY),
        ("TEXTCOLOR",    (0, 0), (-1, 0), C_WHITE),
        ("FONTNAME",     (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",     (0, 0), (-1, 0), 7.5),
        ("ALIGN",        (0, 0), (0, 0), "LEFT"),
        ("ALIGN",        (1, 0), (-1, 0), "CENTER"),
        # ── Sub-header row ──
        ("BACKGROUND",   (0, 1), (-1, 1), C_GREY_SUBH),
        ("TEXTCOLOR",    (0, 1), (-1, 1), C_HOLD),
        ("FONTNAME",     (0, 1), (-1, 1), "Helvetica-Bold"),
        ("FONTSIZE",     (0, 1), (-1, 1), 6.5),
        ("ALIGN",        (0, 1), (0, 1), "LEFT"),
        ("ALIGN",        (1, 1), (-1, 1), "RIGHT"),
        ("LINEBELOW",    (0, 1), (-1, 1), 1.2, C_NAVY),
        # ── Data rows ──
        ("FONTSIZE",     (0, 2), (-1, -1), 7.5),
        ("ALIGN",        (0, 2), (0, -1), "LEFT"),
        ("ALIGN",        (1, 2), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS",(0, 2), (-1, -1), [colors.HexColor("#F8F9FA"), C_WHITE]),
        ("GRID",         (0, 0), (-1, -1), 0.2, colors.HexColor("#dde3ed")),
        ("TOPPADDING",   (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 2),
        ("LEFTPADDING",  (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        # Symbol col font
        ("FONTNAME",     (0, 2), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE",     (0, 2), (0, -1), 8),
    ]

    # Month group header backgrounds and vertical dividers
    elapsed_months   = [m for m in range(1, 13) if date(year, m, 1) <= today]
    completed_months = [m for m in elapsed_months
                        if date(year, m, calendar.monthrange(year, m)[1]) <= today]
    partial_months   = [m for m in elapsed_months if m not in completed_months]
    future_months    = [m for m in range(1, 13) if m not in elapsed_months]

    for m in range(1, 13):
        col_mo  = 1 + (m - 1) * 2      # "Mo." column index
        col_ytd = col_mo + 1            # "YTD" column index

        if m in completed_months:
            bg = C_NAVY
        elif m in partial_months:
            bg = C_PARTIAL
        else:
            bg = C_FUTURE

        t2_s.extend([
            ("BACKGROUND", (col_mo, 0), (col_ytd, 0), bg),
            # Vertical divider before each month group
            ("LINEBEFORE", (col_mo, 0), (col_mo, -1), 0.8,
             colors.HexColor("#8899aa") if m not in future_months else colors.HexColor("#3a3a3a")),
        ])

    # Colour data cells per month per row
    for ri, p in enumerate(sorted_pos, start=2):
        months_data = price_matrix.get(p.symbol, {})
        for m in range(1, 13):
            md      = months_data.get(m, {})
            mo_ret  = md.get("mo_ret_pct")
            ytd_ret = md.get("ytd_ret_pct")
            held    = md.get("held", False)
            col_mo  = 1 + (m - 1) * 2
            col_ytd = col_mo + 1

            if mo_ret is None or not held:
                t2_s.append(("TEXTCOLOR", (col_mo, ri), (col_ytd, ri),
                              colors.HexColor("#c5ccd6")))
                continue

            mo_c  = C_SUCCESS if mo_ret  > 0 else (C_DANGER if mo_ret  < 0 else C_HOLD)
            ytd_c = C_SUCCESS if ytd_ret > 0 else (C_DANGER if ytd_ret < 0 else C_HOLD)
            t2_s.extend([
                ("TEXTCOLOR", (col_mo,  ri), (col_mo,  ri), mo_c),
                ("TEXTCOLOR", (col_ytd, ri), (col_ytd, ri), ytd_c),
                ("FONTNAME",  (col_mo,  ri), (col_mo,  ri), "Helvetica-Bold"),
            ])

    t2_tbl.setStyle(TableStyle(t2_s))
    story.append(t2_tbl)
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph(
        "EoM price sourced from data_cache/consolidated/{symbol}.parquet — last available trading day of each month.  "
        "Greyed header = future month.  Blue header = current partial month.  Navy header = completed month.",
        TINY,
    ))

    # ═══════════════════════════════════════════════════════════════════════
    # MONTHLY OVERVIEW TABLE — one row per month, all metrics in one table
    # ═══════════════════════════════════════════════════════════════════════
    story.append(PageBreak())
    story.append(Paragraph("Monthly Overview", H2))
    story.append(Paragraph(
        "One row per calendar month — key performance and risk metrics at a glance.",
        SMALL,
    ))
    story.append(Spacer(1, 2 * mm))

    mo_hdr = [
        "Month",
        "Return (%)", "P&L (EUR)",
        "Win Rate", "Profit Factor",
        "Open Pos.", "Closed Trades",
        "Active Ret.", "Alpha (ann.)",
        "Beta (β)", "Sharpe", "Info. Ratio",
        "Tracking Err.",
    ]
    mo_rows = [mo_hdr]

    for mb in monthly_reports:
        r   = mb.report
        ab  = r.alpha_beta if r else None
        pf  = (f"{mb.profit_factor:.2f}x"
               if mb.profit_factor not in (float("inf"), 0.0) else
               ("inf" if mb.profit_factor == float("inf") else "—"))
        partial = " *" if not mb.is_complete else ""
        mo_rows.append([
            mb.month_name.replace(f" {year}", "") + partial,
            _fmt_pct(mb.return_pct),
            _fmt_eur(mb.pnl_eur),
            f"{mb.win_rate_pct:.0f}%",
            pf,
            str(r.open_positions  if r else "—"),
            str(r.closed_trades   if r else "—"),
            _fmt_pct(ab.active_return_pct)       if ab else "N/A",
            _fmt_pct(ab.alpha_annualized_pct)    if ab else "N/A",
            f"{ab.beta:.3f}"                     if ab else "N/A",
            f"{ab.sharpe_ratio:.3f}"             if ab else "N/A",
            f"{ab.information_ratio:.3f}"        if ab else "N/A",
            f"{ab.tracking_error_ann:.2f}%"      if ab else "N/A",
        ])

    # YTD totals row
    ytd_ab  = ytd_report.alpha_beta
    ytd_pf  = (f"{ytd_report.profit_factor:.2f}x"
               if ytd_report.profit_factor != float("inf") else "inf")
    mo_rows.append([
        "YTD",
        _fmt_pct(ytd_report.total_return_pct),
        _fmt_eur(ytd_report.total_pnl_eur),
        f"{ytd_report.win_rate_pct:.0f}%",
        ytd_pf,
        str(ytd_report.open_positions),
        str(ytd_report.closed_trades),
        _fmt_pct(ytd_ab.active_return_pct)    if ytd_ab else "N/A",
        _fmt_pct(ytd_ab.alpha_annualized_pct) if ytd_ab else "N/A",
        f"{ytd_ab.beta:.3f}"                  if ytd_ab else "N/A",
        f"{ytd_ab.sharpe_ratio:.3f}"          if ytd_ab else "N/A",
        f"{ytd_ab.information_ratio:.3f}"     if ytd_ab else "N/A",
        f"{ytd_ab.tracking_error_ann:.2f}%"   if ytd_ab else "N/A",
    ])

    mo_w = [
        CONTENT_W * 0.082,   # Month
        CONTENT_W * 0.072,   # Return
        CONTENT_W * 0.090,   # P&L
        CONTENT_W * 0.058,   # Win Rate
        CONTENT_W * 0.072,   # Profit Factor
        CONTENT_W * 0.058,   # Open Pos.
        CONTENT_W * 0.075,   # Closed Trades
        CONTENT_W * 0.072,   # Active Ret.
        CONTENT_W * 0.080,   # Alpha
        CONTENT_W * 0.068,   # Beta
        CONTENT_W * 0.068,   # Sharpe
        CONTENT_W * 0.072,   # Info. Ratio
        CONTENT_W * 0.073,   # Tracking Err.
    ]

    mo_tbl = Table(mo_rows, colWidths=mo_w, repeatRows=1)
    mo_s   = _table_style_base()

    # Colour return + P&L + active return per row
    for ri, mb in enumerate(monthly_reports, start=1):
        ret_c = C_SUCCESS if mb.return_pct >= 0 else C_DANGER
        ab_c  = C_SUCCESS if mb.active_return_pct >= 0 else C_DANGER
        mo_s.extend([
            ("TEXTCOLOR", (1, ri), (2, ri), ret_c),
            ("FONTNAME",  (1, ri), (2, ri), "Helvetica-Bold"),
            ("TEXTCOLOR", (7, ri), (7, ri), ab_c),
        ])

    # YTD totals row — navy background
    ytd_ri  = len(monthly_reports) + 1
    ytd_rc2 = C_SUCCESS if ytd_report.total_return_pct >= 0 else C_DANGER
    mo_s.extend([
        ("BACKGROUND", (0, ytd_ri), (-1, ytd_ri), C_NAVY),
        ("TEXTCOLOR",  (0, ytd_ri), (-1, ytd_ri), C_WHITE),
        ("FONTNAME",   (0, ytd_ri), (-1, ytd_ri), "Helvetica-Bold"),
        ("TEXTCOLOR",  (1, ytd_ri), (2, ytd_ri),  ytd_rc2),
    ])

    # Vertical divider after month name and after closed-trades group
    mo_s.extend([
        ("LINEAFTER", (0,  0), (0,  -1), 1.0, C_HOLD),
        ("LINEAFTER", (6,  0), (6,  -1), 1.0, C_HOLD),
    ])

    mo_tbl.setStyle(TableStyle(mo_s))
    story.append(mo_tbl)

    if any(not mb.is_complete for mb in monthly_reports):
        story.append(Paragraph("* Partial month (data up to today).", TINY))

    # ── FOOTER ───────────────────────────────────────────────────────────────
    story.append(Spacer(1, 4 * mm))
    story.append(HRFlowable(width=CONTENT_W, thickness=0.5, color=C_HOLD))
    story.append(Paragraph(
        f"Multi-Asset Trend Following Strategy — Architecture v3.2 (Feb 2026)  |  "
        f"Script 22: Performance Attribution  |  Year: {year}  |  "
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        TINY,
    ))

    doc.build(story)
    logger.info(f"  Year PDF → {out_path}")
    return out_path


def print_year_summary(
    ytd_report:       "AttributionReport",
    monthly_reports:  "List[MonthlyBreakdown]",
    year:             int,
) -> None:
    """Print year-mode console summary: YTD headline + month-by-month table."""
    SEP = "=" * 78
    sep = "-" * 78
    print("\n" + SEP)
    print(f"  PERFORMANCE ATTRIBUTION — {year} FULL YEAR")
    print(SEP)
    print(f"  YTD Return    : {_fmt_pct(ytd_report.total_return_pct)}")
    print(f"  YTD P&L       : {_fmt_eur(ytd_report.total_pnl_eur)}")
    print(f"  Win Rate      : {ytd_report.win_rate_pct:.1f}%")
    print(f"  Profit Factor : {ytd_report.profit_factor:.2f}x")

    ab = ytd_report.alpha_beta
    if ab:
        print(f"  Alpha (ann.)  : {_fmt_pct(ab.alpha_annualized_pct)}")
        print(f"  Beta          : {ab.beta:.3f}")
        print(f"  Sharpe Ratio  : {ab.sharpe_ratio:.3f}")
        print(f"  Info. Ratio   : {ab.information_ratio:.3f}")
        print(f"  Active Return : {_fmt_pct(ab.active_return_pct)}")

    print("\n" + sep)
    print("  MONTHLY BREAKDOWN")
    print(sep)
    hdr = f"  {'Month':<14}  {'Return':>8}  {'P&L (EUR)':>10}  "
    hdr += f"{'Win%':>5}  {'PF':>5}  {'Alpha':>7}  {'IR':>6}  {'Best':>10}  {'Worst':>10}"
    print(hdr)
    print(f"  {'-'*14}  {'-'*8}  {'-'*10}  {'-'*5}  {'-'*5}  {'-'*7}  {'-'*6}  {'-'*10}  {'-'*10}")

    for mb in monthly_reports:
        partial = " *" if not mb.is_complete else "  "
        ret_str = _fmt_pct(mb.return_pct)
        pf_str  = f"{mb.profit_factor:.2f}x" if mb.profit_factor != float("inf") else "   inf"
        alpha_s = _fmt_pct(mb.alpha_annualized_pct) if mb.alpha_annualized_pct else "  N/A "
        ir_s    = f"{mb.information_ratio:.3f}" if mb.information_ratio else " N/A"
        month_short = mb.month_name.replace(f" {year}", "")
        print(
            f"  {month_short:<14}"
            f"{ret_str:>8}{partial}"
            f"  {_fmt_eur(mb.pnl_eur):>10}  "
            f"{mb.win_rate_pct:>4.0f}%  "
            f"{pf_str:>5}  "
            f"{alpha_s:>7}  "
            f"{ir_s:>6}  "
            f"{mb.top_contributor_symbol:>10}  "
            f"{mb.top_detractor_symbol:>10}"
        )

    print(sep)
    ytd_ab    = ytd_report.alpha_beta
    ytd_alpha = _fmt_pct(ytd_ab.alpha_annualized_pct) if ytd_ab else "  N/A "
    ytd_ir    = f"{ytd_ab.information_ratio:.3f}"     if ytd_ab else " N/A"
    print(
        f"  {'YTD':<16}"
        f"{_fmt_pct(ytd_report.total_return_pct):>8}  "
        f"  {_fmt_eur(ytd_report.total_pnl_eur):>10}  "
        f"{ytd_report.win_rate_pct:>4.0f}%  "
        f"       "
        f"{ytd_alpha:>7}  "
        f"{ytd_ir:>6}"
    )
    if any(not mb.is_complete for mb in monthly_reports):
        print("\n  * Partial month (data up to today)")
    print("\n" + SEP + "\n")


# ============================================================================
# YEAR MODE ORCHESTRATOR
# ============================================================================

def run_year_mode(
    year:            int,
    benchmark:       str,
    account_equity:  float,
    risk_free_rate:  float,
    dry_run:         bool,
) -> int:
    """
    Orchestrate the full year attribution:
      1. Load shared data once (trades, portfolio state, company info)
      2. Compute YTD attribution (Jan 1 through today or Dec 31)
      3. Compute per-month attributions for each elapsed month
      4. Export year JSON, two CSVs (summary + YTD positions), and PDF
      5. Print year summary to console
    """
    logger.info("=" * 70)
    logger.info(f"YEAR MODE — {year}")
    logger.info("=" * 70)

    today      = date.today()
    ytd_start  = date(year, 1, 1)
    ytd_end    = min(date(year, 12, 31), today)

    if ytd_start > today:
        logger.error(f"Year {year} is entirely in the future. Nothing to compute.")
        return 1

    # ── Load shared data (once for all periods) ───────────────────────────────
    logger.info("\n[0] Loading shared data…")
    all_trades     = load_all_trades()
    open_positions = load_portfolio_state()
    company_info   = load_company_info()

    # ── YTD attribution ───────────────────────────────────────────────────────
    logger.info(f"\n[YTD] Computing YTD attribution {ytd_start} → {ytd_end}…")
    ytd_report = run_single_period_attribution(
        start_date      = ytd_start,
        end_date        = ytd_end,
        period_label    = f"{year}-YTD",
        all_trades      = all_trades,
        open_positions  = open_positions,
        company_info    = company_info,
        account_equity  = account_equity,
        benchmark       = benchmark,
        risk_free_rate  = risk_free_rate,
    )

    # ── Per-month attributions ────────────────────────────────────────────────
    month_windows   = enumerate_months(year)
    monthly_reports: List[MonthlyBreakdown] = []

    for idx, (m_start, m_end, m_label, m_name, is_complete) in enumerate(month_windows, 1):
        partial_note = "" if is_complete else " (partial)"
        logger.info(f"\n[{idx}/{len(month_windows)}] {m_name}{partial_note}  {m_start} → {m_end}")
        try:
            m_report = run_single_period_attribution(
                start_date      = m_start,
                end_date        = m_end,
                period_label    = m_label,
                all_trades      = all_trades,
                open_positions  = open_positions,
                company_info    = company_info,
                account_equity  = account_equity,
                benchmark       = benchmark,
                risk_free_rate  = risk_free_rate,
            )
            mb = build_monthly_breakdown(
                report       = m_report,
                month_label  = m_label,
                month_name   = m_name,
                month_start  = m_start,
                month_end    = m_end,
                is_complete  = is_complete,
            )
        except Exception as exc:
            logger.warning(f"  Attribution failed for {m_name}: {exc}")
            mb = MonthlyBreakdown(
                month_label = m_label,
                month_name  = m_name,
                month_start = m_start.isoformat(),
                month_end   = m_end.isoformat(),
                is_complete = is_complete,
                report      = None,
            )
        monthly_reports.append(mb)

    # ── Export all outputs ────────────────────────────────────────────────────
    logger.info("\n[Exports] Writing year outputs…")
    BENCHMARK_DISPLAY = {
        "SPY.US":  "S&P 500 (SPY)",
        "ACWI.US": "MSCI All-Country World (ACWI)",
        "QQQ.US":  "NASDAQ-100 (QQQ)",
    }
    b_name = BENCHMARK_DISPLAY.get(benchmark, benchmark)

    # Collect all unique positions from the YTD report (used by two-table PDF)
    all_positions = ytd_report.positions

    export_year_json(ytd_report, monthly_reports, year, dry_run=dry_run)
    export_year_csv(ytd_report,  monthly_reports, year, dry_run=dry_run)
    generate_year_pdf(
        ytd_report      = ytd_report,
        monthly_reports = monthly_reports,
        year            = year,
        benchmark_name  = b_name,
        all_positions   = all_positions,
        all_trades      = all_trades,
        dry_run         = dry_run,
    )

    # ── Console summary ───────────────────────────────────────────────────────
    print_year_summary(ytd_report, monthly_reports, year)
    return 0


# ============================================================================
# MAIN ORCHESTRATOR
# ============================================================================

def _run_for_strategy(strategy: "StrategyDef", args) -> int:
    """Run Script 22 for one strategy with namespaced I/O paths."""
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


def _run_core(args, strategy_name: str = '') -> int:

    # ── YEAR MODE — branch early ──────────────────────────────────────────────
    if getattr(args, "year", None):
        try:
            year_int = int(args.year)
        except (ValueError, TypeError):
            print(f"ERROR: Invalid --year value '{args.year}'. Use a 4-digit year e.g. 2026.")
            return 1

        setup_logging(str(year_int))
        logger.info("=" * 70)
        logger.info("PERFORMANCE ATTRIBUTION — Script 22  [YEAR MODE]")
        logger.info("Architecture v3.2 (Feb 2026)")
        logger.info("=" * 70)
        logger.info(f"Year        : {year_int}")
        logger.info(f"Benchmark   : {args.benchmark}")
        logger.info(f"Equity (EUR): {args.account_equity:,.2f}")
        logger.info(f"Dry run     : {args.dry_run}")
        return run_year_mode(
            year           = year_int,
            benchmark      = args.benchmark,
            account_equity = args.account_equity,
            risk_free_rate = args.risk_free_rate,
            dry_run        = args.dry_run,
        )

    # ── SINGLE-PERIOD MODE (--month / custom range / default) ─────────────────
    start_date, end_date, period_label = resolve_dates(args)
    setup_logging(period_label)

    logger.info("=" * 70)
    logger.info("PERFORMANCE ATTRIBUTION — Script 22")
    logger.info("Architecture v3.2 (Feb 2026)")
    logger.info("=" * 70)
    logger.info(f"Period      : {start_date}  →  {end_date}  ({period_label})")
    logger.info(f"Benchmark   : {args.benchmark}")
    logger.info(f"Equity (EUR): {args.account_equity:,.2f}")
    logger.info(f"Dry run     : {args.dry_run}")

    account_equity = args.account_equity if args.account_equity > 0 else 0.0

    # ── Load shared data ──────────────────────────────────────────────────────
    logger.info("\n[1/6] Loading data sources…")
    all_trades     = load_all_trades()
    open_positions = load_portfolio_state()
    company_info   = load_company_info()

    period_trades = load_trade_ledger(start_date, end_date)
    if not period_trades and not open_positions:
        logger.warning(
            "No trades found for the period and no open positions. "
            "Ensure trade_ledger.jsonl and portfolio_state.json are populated "
            "by running Scripts 11 and 13 first."
        )

    # ── Run attribution ───────────────────────────────────────────────────────
    logger.info("\n[2-5/6] Running attribution engine…")
    report = run_single_period_attribution(
        start_date      = start_date,
        end_date        = end_date,
        period_label    = period_label,
        all_trades      = all_trades,
        open_positions  = open_positions,
        company_info    = company_info,
        account_equity  = account_equity,
        benchmark       = args.benchmark,
        risk_free_rate  = args.risk_free_rate,
    )

    # ── Export outputs ────────────────────────────────────────────────────────
    logger.info("\n[6/6] Exporting outputs…")
    json_path = export_json(report, dry_run=args.dry_run)
    csv_path  = export_csv(report,  dry_run=args.dry_run)
    pdf_path  = generate_pdf(report, dry_run=args.dry_run)

    # ── Console summary ───────────────────────────────────────────────────────
    print_summary(report)

    logger.info("Attribution complete.")
    if not args.dry_run:
        if json_path:
            logger.info(f"  → JSON : {json_path}")
        if csv_path:
            logger.info(f"  → CSV  : {csv_path}")
        if pdf_path:
            logger.info(f"  → PDF  : {pdf_path}")
    return 0


def main() -> int:
    args = parse_arguments()
    logger.info("=" * 70)
    logger.info("Script 22 -- Architecture v3.9 (Mar 2026)")
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
    sys.exit(main())
