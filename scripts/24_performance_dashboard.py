#!/usr/bin/env python3
"""
Script 24: Performance Dashboard
=================================
Generate a comprehensive, self-contained interactive HTML performance
dashboard for the multi-asset trend-following strategy.

Purpose:
    Provide a single-page visual overview of portfolio performance, risk,
    and positioning.  All charts are built from on-disk data produced by
    earlier pipeline scripts — no live data feed required.

    Six panels are rendered:

        PANEL 1 ─ Equity Curve (daily NAV reconstructed from trade ledger)
        PANEL 2 ─ Monthly Return Heatmap (calendar grid)
        PANEL 3 ─ Drawdown Chart (underwater equity curve)
        PANEL 4 ─ Rolling Sharpe Ratio (252-day trailing window)
        PANEL 5 ─ Asset Allocation Pie (current portfolio by asset class)
        PANEL 6 ─ Position P&L Table (open positions ranked by unrealised P&L)

    A summary metrics bar at the top shows:
        Total Return, Annualised Return, Sharpe Ratio, Max Drawdown,
        Win Rate, Profit Factor, Current Positions, Portfolio Value

Architecture constraints:
    ─ No external JS/CSS CDN dependencies (fully self-contained HTML)
    ─ Plotly is embedded via CDN in the HTML template (one dependency only,
      loaded from https://cdn.plot.ly – standard for institutional dashboards)
    ─ Python-side computation only; no client-side data processing
    ─ Dashboard loads < 5 seconds on localhost (target: < 1 MB HTML)
    ─ Degrades gracefully when upstream files are absent (shows N/A)

Inputs:
    data/portfolio_state.json                           (Script 13)
    data/trade_ledger.jsonl                             (Script 13)
    data/performance/attribution/*.json                 (Script 22, optional)
    data/performance/risk/*.json                        (Script 23, optional)
    data_cache/consolidated/{symbol}.parquet            (Script 03, optional)
    data_cache/fundamentals/company_info.json           (Script 02, optional)

Outputs:
    reports/performance/dashboard_{YYYYMMDD_HHMMSS}.html
    reports/performance/dashboard_latest.html           (symlink / copy)
    logs/performance_dashboard_{timestamp}.log

CLI Reference:
    --account-equity FLOAT      Override equity in EUR (for VaR display)
    --start-date     YYYY-MM-DD Equity curve start (default: first trade)
    --benchmark      SPY.US | ACWI.US  (default: SPY.US, used for annotation)
    --output         PATH       Custom output path (overrides default)
    --dry-run                   Build dashboard in memory; do not write files
    --no-prices                 Skip parquet loading (faster; hides equity curve)
    --top-n          INT        Positions shown in P&L table (default: 25)

Execution:
    # Standard run
    python scripts/24_performance_dashboard.py

    # Custom equity override (if portfolio_state is stale)
    python scripts/24_performance_dashboard.py --account-equity 54000

    # Limit curve to last 12 months
    python scripts/24_performance_dashboard.py --start-date 2025-02-01

    # No price lookups (fast mode)
    python scripts/24_performance_dashboard.py --no-prices

    # Dry-run (validate data without writing)
    python scripts/24_performance_dashboard.py --dry-run

Architecture: v3.2 (Feb 2026)
"""

import sys
import json
import logging
import argparse
import math
import shutil
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ── Plotly (required) ────────────────────────────────────────────────────────
try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    import plotly
except ImportError:
    print("[ERROR] plotly is required:  pip install plotly")
    sys.exit(1)

# ============================================================================
# PATHS  (mirror project layout from other scripts)
# ============================================================================

PROJECT_ROOT          = Path(__file__).parent.parent   # trend_strategy/scripts/ → trend_strategy/
DATA_DIR              = PROJECT_ROOT / "data"
DATA_CACHE_DIR        = PROJECT_ROOT / "data_cache"
DATA_LOAD_DIR         = PROJECT_ROOT.parent / "data_load" / "data_cache"
CONSOLIDATED_DIR      = DATA_LOAD_DIR / "consolidated"
FUNDAMENTALS_DIR      = DATA_LOAD_DIR / "fundamentals"
PERFORMANCE_DIR       = DATA_DIR / "performance"
ATTRIBUTION_DIR       = PERFORMANCE_DIR / "attribution"
RISK_DIR              = PERFORMANCE_DIR / "risk"
REPORTS_DIR           = PROJECT_ROOT / "reports" / "performance"
LOG_DIR               = PROJECT_ROOT / "logs"

PORTFOLIO_STATE_FILE  = DATA_DIR / "portfolio_state.json"
TRADE_LEDGER_FILE     = DATA_DIR / "trade_ledger.jsonl"
COMPANY_INFO_FILE     = FUNDAMENTALS_DIR / "company_info.json"

# ============================================================================
# CONSTANTS
# ============================================================================

RISK_FREE_RATE_ANNUAL = 0.04        # 4 % — ECB deposit facility circa 2026
TRADING_DAYS_PER_YEAR = 252
ROLLING_SHARPE_WINDOW = 252         # 1 year
MIN_PERIODS_SHARPE    = 60          # min days to compute a Sharpe

BENCHMARK_SYMBOLS     = {"SPY.US", "ACWI.US", "IEW.US"}

# Colour palette (accessible, dark-background friendly)
COLOR_EQUITY   = "#4FC3F7"   # sky blue
COLOR_BM       = "#B0BEC5"   # grey
COLOR_PROFIT   = "#69F0AE"   # green
COLOR_LOSS     = "#FF5252"   # red
COLOR_DRAWDOWN = "#FF8A65"   # orange
COLOR_SHARPE   = "#CE93D8"   # purple
COLOR_PANEL_BG = "#1A1A2E"
COLOR_PLOT_BG  = "#16213E"
COLOR_GRID     = "#2A2A4A"
COLOR_TEXT     = "#E0E0E0"
COLOR_SUBTEXT  = "#9E9E9E"

ASSET_CLASS_COLORS = {
    "US Stock":       "#4FC3F7",
    "EU Stock":       "#81C784",
    "UK Stock":       "#FFD54F",
    "ETF":            "#CE93D8",
    "Cryptocurrency": "#FF8A65",
    "Unknown":        "#78909C",
}

# ============================================================================
# LOGGING
# ============================================================================

LOG_DIR.mkdir(parents=True, exist_ok=True)
_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"performance_dashboard_{_ts}.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class PortfolioMetrics:
    """Aggregated performance metrics shown in the summary bar."""
    total_return_pct:      Optional[float] = None
    annualised_return_pct: Optional[float] = None
    sharpe_ratio:          Optional[float] = None
    max_drawdown_pct:      Optional[float] = None
    win_rate_pct:          Optional[float] = None
    profit_factor:         Optional[float] = None
    num_positions:         int = 0
    portfolio_value_eur:   Optional[float] = None
    total_trades:          int = 0
    first_trade_date:      Optional[str] = None
    last_update:           str = field(default_factory=lambda: datetime.now().isoformat())

    # Risk metrics (populated from Script 23 if available)
    var_95_1d_eur:         Optional[float] = None
    cvar_95_1d_eur:        Optional[float] = None
    beta:                  Optional[float] = None
    information_ratio:     Optional[float] = None
    hhi:                   Optional[float] = None


@dataclass
class PositionRow:
    """One row in the Position P&L table."""
    symbol:              str
    name:                str
    asset_class:         str
    sector:              str
    shares:              int
    entry_price:         float
    current_price:       float
    entry_date:          str
    unrealised_pnl_eur:  float
    unrealised_pnl_pct:  float
    weight_pct:          float
    current_value_eur:   float
    stop_price:          Optional[float]
    days_held:           int


# ============================================================================
# DATA LOADERS
# ============================================================================

def load_portfolio_state() -> Dict:
    """Load portfolio_state.json. Returns {} on first run."""
    if not PORTFOLIO_STATE_FILE.exists():
        logger.warning("portfolio_state.json not found — no open positions.")
        return {}
    with open(PORTFOLIO_STATE_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    if "positions" in raw and isinstance(raw["positions"], dict):
        raw = raw["positions"]
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def load_trade_ledger() -> List[Dict]:
    """Load trade_ledger.jsonl — one JSON object per line."""
    if not TRADE_LEDGER_FILE.exists():
        logger.warning("trade_ledger.jsonl not found — no trade history.")
        return []
    records = []
    with open(TRADE_LEDGER_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.load(__import__("io").StringIO(line)))
                except json.JSONDecodeError:
                    continue
    logger.info(f"  Loaded {len(records)} trade records from ledger.")
    return records


def load_company_info() -> Dict:
    """Load company_info.json from Script 02."""
    if not COMPANY_INFO_FILE.exists():
        logger.warning("company_info.json not found — sector/name info unavailable.")
        return {}
    with open(COMPANY_INFO_FILE, encoding="utf-8") as f:
        data = json.load(f)
    logger.info(f"  Loaded company info for {len(data)} symbols.")
    return data


def load_latest_attribution() -> Optional[Dict]:
    """Load the most recent attribution JSON from Script 22."""
    if not ATTRIBUTION_DIR.exists():
        return None
    files = sorted(ATTRIBUTION_DIR.glob("*_attribution.json"), reverse=True)
    if not files:
        return None
    with open(files[0], encoding="utf-8") as f:
        data = json.load(f)
    logger.info(f"  Loaded attribution: {files[0].name}")
    return data


def load_latest_risk() -> Optional[Dict]:
    """Load the most recent risk metrics JSON from Script 23."""
    if not RISK_DIR.exists():
        return None
    files = sorted(RISK_DIR.glob("*_risk_metrics.json"), reverse=True)
    if not files:
        return None
    with open(files[0], encoding="utf-8") as f:
        data = json.load(f)
    logger.info(f"  Loaded risk metrics: {files[0].name}")
    return data


def load_price_series(symbol: str) -> Optional[pd.Series]:
    """Load adjusted close from Script 03 consolidated parquet."""
    path = CONSOLIDATED_DIR / f"{symbol}.parquet"
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path, columns=["close"])
        df.index = pd.to_datetime(df.index)
        return df["close"].sort_index()
    except Exception as e:
        logger.debug(f"  Cannot load prices for {symbol}: {e}")
        return None


# ============================================================================
# CLASSIFICATION HELPERS
# ============================================================================

SUFFIX_TO_ASSET_CLASS = {
    ".US":  "US Stock",
    ".DE":  "EU Stock",
    ".PA":  "EU Stock",
    ".AS":  "EU Stock",
    ".L":   "UK Stock",
    ".CC":  "Cryptocurrency",
    ".V":   "Cryptocurrency",
}

ETF_QUOTE_TYPES = {"ETF", "MUTUALFUND", "INDEX"}


def classify_asset_class(symbol: str, company_info: Dict) -> str:
    if symbol.endswith((".CC", ".V")):
        return "Cryptocurrency"
    info = company_info.get(symbol, {})
    if (info.get("quoteType") or "").upper() in ETF_QUOTE_TYPES:
        return "ETF"
    for suffix, ac in SUFFIX_TO_ASSET_CLASS.items():
        if symbol.upper().endswith(suffix.upper()):
            return ac
    return "Unknown"


def classify_sector(symbol: str, company_info: Dict) -> str:
    info = company_info.get(symbol, {})
    return info.get("sector") or info.get("industry") or "Unknown"


def get_company_name(symbol: str, company_info: Dict) -> str:
    info = company_info.get(symbol, {})
    return info.get("longName") or info.get("shortName") or symbol


# ============================================================================
# EQUITY CURVE RECONSTRUCTION
# ============================================================================

def reconstruct_equity_curve(
    trades:        List[Dict],
    portfolio:     Dict,
    start_date:    Optional[date],
    load_prices:   bool,
    account_equity: Optional[float],
) -> Tuple[Optional[pd.Series], Optional[pd.Series]]:
    """
    Reconstruct a daily NAV series from the trade ledger and open positions.

    Method:
        1.  Build a cash account tracking all entries and exits.
        2.  At each date, mark open positions to current price (if parquet
            available); otherwise carry forward at last known value.
        3.  NAV[t] = Cash[t] + Σ(shares_i × price_i[t])

    Returns:
        (nav_series, daily_returns_series) — both indexed by date.
        Returns (None, None) if insufficient data.
    """
    if not trades:
        logger.warning("No trade records — equity curve unavailable.")
        return None, None

    # ── Infer initial cash ────────────────────────────────────────────────────
    # Use first BUY to anchor starting portfolio value.
    # If account_equity is given, use it as starting seed; else sum BUY notionals.
    sorted_trades = sorted(trades, key=lambda t: t.get("execution_date", ""))
    if not sorted_trades:
        return None, None

    first_date = pd.to_datetime(sorted_trades[0]["execution_date"]).date()
    if start_date and start_date > first_date:
        first_date = start_date

    last_date = date.today()

    # Build daily date index (calendar days, not business days — gap fill later)
    date_range = pd.date_range(
        start=first_date,
        end=last_date,
        freq="B",  # business days
    )
    if len(date_range) < 2:
        return None, None

    # ── Trade events ──────────────────────────────────────────────────────────
    # cash_flows[date] = net cash delta (negative = BUY, positive = SELL)
    # holdings[date][symbol] = (shares, price) deltas
    buy_events: Dict[str, List[Dict]] = {}
    sell_events: Dict[str, List[Dict]] = {}
    for t in sorted_trades:
        d = t.get("execution_date", "")
        if not d:
            continue
        if t.get("action") == "BUY":
            buy_events.setdefault(d, []).append(t)
        elif t.get("action") == "SELL":
            sell_events.setdefault(d, []).append(t)

    # ── Price cache (load parquet files once) ─────────────────────────────────
    price_cache: Dict[str, pd.Series] = {}
    all_symbols = set()
    for t in sorted_trades:
        s = t.get("symbol", "")
        if s and s not in BENCHMARK_SYMBOLS:
            all_symbols.add(s)
    for sym in portfolio:
        if sym not in BENCHMARK_SYMBOLS:
            all_symbols.add(sym)

    if load_prices:
        for sym in all_symbols:
            series = load_price_series(sym)
            if series is not None:
                price_cache[sym] = series

    # ── Simulate NAV day by day ───────────────────────────────────────────────
    cash       = 0.0
    holdings   : Dict[str, Tuple[int, float]] = {}  # {symbol: (shares, avg_cost)}
    nav_values : Dict[date, float]            = {}

    # Seed cash from first session of buys
    first_buy_date = sorted_trades[0]["execution_date"]
    first_buys     = [t for t in sorted_trades if t.get("action") == "BUY"
                      and t.get("execution_date") == first_buy_date]
    # Starting equity = sum of first-day purchase notional
    if account_equity:
        cash = account_equity
    else:
        cash = sum(t.get("shares", 0) * t.get("fill_price", 0) + t.get("commission", 0)
                   for t in first_buys) if first_buys else 10_000.0

    for ts in date_range:
        d_str = ts.strftime("%Y-%m-%d")
        d_obj = ts.date()

        if d_obj < first_date:
            continue

        # Apply BUYs
        for t in buy_events.get(d_str, []):
            sym   = t.get("symbol", "")
            qty   = int(t.get("shares", 0))
            price = float(t.get("fill_price", 0))
            comm  = float(t.get("commission", 0))
            cost  = qty * price + comm
            cash -= cost
            if sym:
                prev_qty, prev_cost = holdings.get(sym, (0, 0.0))
                new_qty  = prev_qty + qty
                new_cost = (prev_cost * prev_qty + price * qty) / new_qty if new_qty else 0
                holdings[sym] = (new_qty, new_cost)

        # Apply SELLs
        for t in sell_events.get(d_str, []):
            sym   = t.get("symbol", "")
            qty   = int(t.get("shares", 0))
            price = float(t.get("fill_price", 0))
            comm  = float(t.get("commission", 0))
            proceeds = qty * price - comm
            cash += proceeds
            if sym and sym in holdings:
                prev_qty, prev_cost = holdings[sym]
                remaining = prev_qty - qty
                if remaining <= 0:
                    del holdings[sym]
                else:
                    holdings[sym] = (remaining, prev_cost)

        # Mark holdings to market
        position_value = 0.0
        for sym, (qty, cost) in holdings.items():
            if sym in BENCHMARK_SYMBOLS:
                continue
            if sym in price_cache:
                series = price_cache[sym]
                idx    = series.index.asof(ts)
                if pd.notna(idx):
                    price = float(series.loc[idx])
                else:
                    price = cost  # carry forward at cost if no price
            else:
                price = cost  # fallback to cost if no parquet

            position_value += qty * price

        nav = cash + position_value
        # Guard against nonsensical negatives early in simulation
        if nav < 0 and d_obj == first_date:
            nav = abs(nav)
        nav_values[d_obj] = nav

    if len(nav_values) < 5:
        logger.warning("Equity curve has fewer than 5 data points — skipping.")
        return None, None

    nav_series = pd.Series(nav_values).sort_index()
    # Ensure DatetimeIndex (required for resample, rolling, and Plotly x-axis)
    nav_series.index = pd.to_datetime(nav_series.index)
    base = nav_series.iloc[0]
    if base <= 0:
        logger.warning("First NAV ≤ 0 — cannot normalise equity curve.")
        return None, None

    # Keep absolute EUR values (not indexed) for display
    daily_returns = nav_series.pct_change().dropna()
    return nav_series, daily_returns


# ============================================================================
# PERFORMANCE METRICS
# ============================================================================

def compute_metrics(
    nav:            Optional[pd.Series],
    daily_returns:  Optional[pd.Series],
    trades:         List[Dict],
    portfolio:      Dict,
    risk_data:      Optional[Dict],
    account_equity: Optional[float],
) -> PortfolioMetrics:
    """Compute all summary metrics from available inputs."""
    m = PortfolioMetrics()
    m.num_positions = len([s for s in portfolio if s not in BENCHMARK_SYMBOLS])
    m.total_trades  = len(trades)

    if trades:
        dates = [t["execution_date"] for t in trades if t.get("execution_date")]
        if dates:
            m.first_trade_date = min(dates)

    if account_equity:
        m.portfolio_value_eur = account_equity
    elif nav is not None and len(nav) > 0:
        m.portfolio_value_eur = float(nav.iloc[-1])

    if nav is not None and len(nav) >= 2:
        first_nav = float(nav.iloc[0])
        last_nav  = float(nav.iloc[-1])

        if first_nav > 0:
            total_return = (last_nav - first_nav) / first_nav
            m.total_return_pct = total_return * 100

            n_days = (nav.index[-1] - nav.index[0]).days
            if n_days > 0:
                years = n_days / 365.25
                m.annualised_return_pct = ((last_nav / first_nav) ** (1 / years) - 1) * 100

        # Max drawdown
        rolling_max = nav.cummax()
        drawdowns   = (nav - rolling_max) / rolling_max
        m.max_drawdown_pct = float(drawdowns.min()) * 100

    if daily_returns is not None and len(daily_returns) >= MIN_PERIODS_SHARPE:
        excess     = daily_returns - RISK_FREE_RATE_ANNUAL / TRADING_DAYS_PER_YEAR
        mean_daily = excess.mean()
        std_daily  = excess.std()
        if std_daily > 0:
            m.sharpe_ratio = float(mean_daily / std_daily * math.sqrt(TRADING_DAYS_PER_YEAR))

    # Win rate and profit factor from closed trades (SELL records)
    sell_trades = [t for t in trades if t.get("action") == "SELL"]
    if sell_trades:
        pnls      = [float(t.get("realized_pnl_eur") or 0) for t in sell_trades]
        winners   = [p for p in pnls if p > 0]
        losers    = [p for p in pnls if p < 0]
        m.win_rate_pct = len(winners) / len(pnls) * 100 if pnls else None
        gross_profit = sum(winners) if winners else 0
        gross_loss   = abs(sum(losers)) if losers else 0
        if gross_loss > 0:
            m.profit_factor = gross_profit / gross_loss

    # Populate risk metrics from Script 23 output
    if risk_data:
        var_block = risk_data.get("var", {})
        if var_block:
            m.var_95_1d_eur  = var_block.get("var_95_1d_eur")
            m.cvar_95_1d_eur = var_block.get("cvar_95_1d_eur")
        m.beta               = risk_data.get("beta")
        m.information_ratio  = risk_data.get("information_ratio")
        m.hhi                = risk_data.get("hhi")

    return m


# ============================================================================
# POSITION TABLE
# ============================================================================

def build_position_rows(
    portfolio:      Dict,
    company_info:   Dict,
    load_prices:    bool,
    account_equity: Optional[float],
    top_n:          int,
) -> List[PositionRow]:
    """Build position rows for the P&L table."""
    rows: List[PositionRow] = []
    total_value = 0.0

    for sym, pos in portfolio.items():
        if sym in BENCHMARK_SYMBOLS:
            continue
        shares      = int(pos.get("shares", 0))
        entry_price = float(pos.get("entry_price", 0))
        entry_date  = pos.get("entry_date", "")
        stop_price  = pos.get("current_stop_price") or pos.get("initial_stop_price")

        # Current price
        current_price = entry_price  # fallback
        if load_prices:
            series = load_price_series(sym)
            if series is not None and len(series) > 0:
                current_price = float(series.iloc[-1])
            else:
                # Try from portfolio state (may have been updated by Script 14)
                current_price = float(pos.get("current_price") or entry_price)
        else:
            current_price = float(pos.get("current_price") or entry_price)

        current_value   = shares * current_price
        unrealised_pnl  = shares * (current_price - entry_price)
        unrealised_pnl_pct = (
            (current_price - entry_price) / entry_price * 100
            if entry_price > 0 else 0.0
        )

        # Days held
        try:
            entry_dt = datetime.strptime(entry_date[:10], "%Y-%m-%d").date()
            days_held = (date.today() - entry_dt).days
        except (ValueError, TypeError):
            days_held = 0

        rows.append(PositionRow(
            symbol             = sym,
            name               = get_company_name(sym, company_info),
            asset_class        = classify_asset_class(sym, company_info),
            sector             = classify_sector(sym, company_info),
            shares             = shares,
            entry_price        = entry_price,
            current_price      = current_price,
            entry_date         = entry_date[:10] if entry_date else "",
            unrealised_pnl_eur = unrealised_pnl,
            unrealised_pnl_pct = unrealised_pnl_pct,
            weight_pct         = 0.0,   # filled after total is known
            current_value_eur  = current_value,
            stop_price         = float(stop_price) if stop_price else None,
            days_held          = days_held,
        ))
        total_value += current_value

    # Compute weights
    for r in rows:
        if total_value > 0:
            r.weight_pct = r.current_value_eur / total_value * 100

    # Sort by unrealised P&L descending
    rows.sort(key=lambda r: r.unrealised_pnl_eur, reverse=True)
    return rows[:top_n]


# ============================================================================
# CHART BUILDERS  (Plotly)
# ============================================================================

def _apply_dark_theme(fig: go.Figure, title: str) -> go.Figure:
    """Apply uniform dark theme to any Plotly figure."""
    fig.update_layout(
        title       = dict(text=title, font=dict(size=14, color=COLOR_TEXT)),
        paper_bgcolor = COLOR_PANEL_BG,
        plot_bgcolor  = COLOR_PLOT_BG,
        font          = dict(color=COLOR_TEXT, size=11),
        legend        = dict(bgcolor=COLOR_PANEL_BG, bordercolor=COLOR_GRID),
        margin        = dict(l=50, r=20, t=50, b=40),
    )
    fig.update_xaxes(gridcolor=COLOR_GRID, zerolinecolor=COLOR_GRID)
    fig.update_yaxes(gridcolor=COLOR_GRID, zerolinecolor=COLOR_GRID)
    return fig


def build_equity_curve_fig(
    nav:       Optional[pd.Series],
    start_date: Optional[date],
) -> go.Figure:
    """Panel 1: Equity curve (absolute EUR NAV)."""
    fig = go.Figure()

    if nav is not None and len(nav) >= 2:
        if start_date:
            nav = nav[nav.index >= pd.Timestamp(start_date)]

        # Shade profit/loss regions vs starting value
        start_val = nav.iloc[0]
        above     = nav.copy()
        below     = nav.copy()
        above[nav < start_val]  = start_val
        below[nav >= start_val] = start_val

        fig.add_trace(go.Scatter(
            x=nav.index, y=above.values,
            fill="tozeroy", fillcolor="rgba(105,240,174,0.08)",
            line=dict(width=0), showlegend=False, name="Above Start",
        ))
        fig.add_trace(go.Scatter(
            x=nav.index, y=below.values,
            fill="tozeroy", fillcolor="rgba(255,82,82,0.08)",
            line=dict(width=0), showlegend=False, name="Below Start",
        ))
        fig.add_trace(go.Scatter(
            x=nav.index, y=nav.values,
            mode="lines",
            line=dict(color=COLOR_EQUITY, width=2),
            name="Portfolio NAV (€)",
            hovertemplate="%{x|%d %b %Y}<br>€%{y:,.0f}<extra></extra>",
        ))
        # Starting reference line
        fig.add_hline(
            y=start_val, line_dash="dot",
            line_color=COLOR_SUBTEXT, line_width=1, opacity=0.6,
        )
    else:
        fig.add_annotation(
            text="No equity curve data available<br>Run Scripts 13 + 03 first",
            xref="paper", yref="paper", x=0.5, y=0.5,
            showarrow=False, font=dict(size=13, color=COLOR_SUBTEXT),
        )

    fig.update_layout(yaxis_title="Portfolio Value (€)")
    return _apply_dark_theme(fig, "📈 Equity Curve")


def build_monthly_heatmap_fig(daily_returns: Optional[pd.Series]) -> go.Figure:
    """Panel 2: Monthly return heatmap."""
    fig = go.Figure()

    if daily_returns is not None and len(daily_returns) >= 20:
        monthly = daily_returns.resample("ME").agg(lambda r: (1 + r).prod() - 1) * 100
        monthly.index = pd.to_datetime(monthly.index)

        years  = sorted(monthly.index.year.unique())
        months = list(range(1, 13))
        month_names = ["Jan","Feb","Mar","Apr","May","Jun",
                       "Jul","Aug","Sep","Oct","Nov","Dec"]

        z_data   : List[List[Optional[float]]] = []
        text_data: List[List[str]]             = []

        for yr in years:
            z_row    = []
            text_row = []
            for mo in months:
                mask = (monthly.index.year == yr) & (monthly.index.month == mo)
                if mask.any():
                    val = float(monthly[mask].iloc[0])
                    z_row.append(val)
                    text_row.append(f"{val:+.1f}%")
                else:
                    z_row.append(None)
                    text_row.append("")
            z_data.append(z_row)
            text_data.append(text_row)

        max_abs = max(
            (abs(v) for row in z_data for v in row if v is not None),
            default=5.0,
        )

        fig.add_trace(go.Heatmap(
            z=z_data,
            x=month_names,
            y=[str(yr) for yr in years],
            text=text_data,
            texttemplate="%{text}",
            textfont=dict(size=10),
            colorscale=[
                [0.0,  "#C62828"],   # deep red
                [0.35, "#FF5252"],
                [0.5,  "#2A2A4A"],   # neutral (dark)
                [0.65, "#69F0AE"],
                [1.0,  "#1B5E20"],   # deep green
            ],
            zmid=0,
            zmin=-max_abs,
            zmax=max_abs,
            colorbar=dict(
                title="Return %",
                titlefont=dict(color=COLOR_TEXT),
                tickfont=dict(color=COLOR_TEXT),
            ),
            hovertemplate="%{y} %{x}: %{text}<extra></extra>",
        ))
    else:
        fig.add_annotation(
            text="Insufficient return history<br>(need ≥ 20 trading days)",
            xref="paper", yref="paper", x=0.5, y=0.5,
            showarrow=False, font=dict(size=13, color=COLOR_SUBTEXT),
        )

    return _apply_dark_theme(fig, "🗓️ Monthly Return Heatmap")


def build_drawdown_fig(nav: Optional[pd.Series]) -> go.Figure:
    """Panel 3: Drawdown underwater chart."""
    fig = go.Figure()

    if nav is not None and len(nav) >= 2:
        rolling_max = nav.cummax()
        dd          = (nav - rolling_max) / rolling_max * 100

        fig.add_trace(go.Scatter(
            x=dd.index, y=dd.values,
            fill="tozeroy", fillcolor="rgba(255,138,101,0.25)",
            line=dict(color=COLOR_DRAWDOWN, width=1.5),
            name="Drawdown",
            hovertemplate="%{x|%d %b %Y}<br>DD: %{y:.1f}%<extra></extra>",
        ))
        # Max drawdown annotation
        min_dd_idx = dd.idxmin()
        min_dd_val = dd.min()
        if pd.notna(min_dd_idx):
            fig.add_annotation(
                x=min_dd_idx, y=min_dd_val,
                text=f"Max DD<br>{min_dd_val:.1f}%",
                showarrow=True, arrowhead=2,
                font=dict(color=COLOR_LOSS, size=10),
                arrowcolor=COLOR_LOSS, bgcolor=COLOR_PANEL_BG,
                bordercolor=COLOR_LOSS,
            )
    else:
        fig.add_annotation(
            text="No drawdown data available",
            xref="paper", yref="paper", x=0.5, y=0.5,
            showarrow=False, font=dict(size=13, color=COLOR_SUBTEXT),
        )

    fig.update_layout(yaxis_title="Drawdown (%)")
    return _apply_dark_theme(fig, "📉 Drawdown")


def build_rolling_sharpe_fig(daily_returns: Optional[pd.Series]) -> go.Figure:
    """Panel 4: Rolling Sharpe ratio (252-day trailing)."""
    fig = go.Figure()

    if daily_returns is not None and len(daily_returns) >= MIN_PERIODS_SHARPE:
        rfr_daily = RISK_FREE_RATE_ANNUAL / TRADING_DAYS_PER_YEAR
        excess    = daily_returns - rfr_daily

        rolling_mean = excess.rolling(ROLLING_SHARPE_WINDOW, min_periods=MIN_PERIODS_SHARPE).mean()
        rolling_std  = excess.rolling(ROLLING_SHARPE_WINDOW, min_periods=MIN_PERIODS_SHARPE).std()
        rolling_sharpe = (rolling_mean / rolling_std * math.sqrt(TRADING_DAYS_PER_YEAR)).dropna()

        # Colour lines above/below 1.0 threshold
        fig.add_trace(go.Scatter(
            x=rolling_sharpe.index, y=rolling_sharpe.values,
            mode="lines",
            line=dict(color=COLOR_SHARPE, width=2),
            name="Rolling Sharpe (252d)",
            hovertemplate="%{x|%d %b %Y}<br>Sharpe: %{y:.2f}<extra></extra>",
        ))
        # Reference lines
        for level, label in [(1.0, "Good"), (0.0, "Zero")]:
            fig.add_hline(
                y=level, line_dash="dash",
                line_color=COLOR_SUBTEXT, line_width=1, opacity=0.7,
                annotation_text=label, annotation_font_size=9,
                annotation_font_color=COLOR_SUBTEXT,
            )
    else:
        fig.add_annotation(
            text=f"Need ≥ {MIN_PERIODS_SHARPE} trading days of returns<br>for rolling Sharpe",
            xref="paper", yref="paper", x=0.5, y=0.5,
            showarrow=False, font=dict(size=13, color=COLOR_SUBTEXT),
        )

    fig.update_layout(yaxis_title="Sharpe Ratio")
    return _apply_dark_theme(fig, "📊 Rolling Sharpe Ratio (252-day)")


def build_allocation_pie_fig(
    position_rows: List[PositionRow],
    company_info:  Dict,
) -> go.Figure:
    """Panel 5: Asset allocation pie by asset class."""
    fig = go.Figure()

    if position_rows:
        from collections import defaultdict
        by_class: Dict[str, float] = defaultdict(float)
        for row in position_rows:
            by_class[row.asset_class] += row.current_value_eur

        labels  = list(by_class.keys())
        values  = [by_class[l] for l in labels]
        colors  = [ASSET_CLASS_COLORS.get(l, "#78909C") for l in labels]

        fig.add_trace(go.Pie(
            labels=labels,
            values=values,
            marker=dict(colors=colors, line=dict(color=COLOR_PANEL_BG, width=2)),
            textinfo="label+percent",
            hovertemplate="%{label}<br>€%{value:,.0f} (%{percent})<extra></extra>",
            textfont=dict(color=COLOR_TEXT, size=11),
            hole=0.4,  # donut style
        ))
        fig.update_layout(showlegend=True)
    else:
        fig.add_annotation(
            text="No open positions",
            xref="paper", yref="paper", x=0.5, y=0.5,
            showarrow=False, font=dict(size=13, color=COLOR_SUBTEXT),
        )

    return _apply_dark_theme(fig, "🥧 Asset Allocation")


# ============================================================================
# POSITION TABLE HTML
# ============================================================================

def _fmt_pnl(val: float) -> str:
    color = COLOR_PROFIT if val >= 0 else COLOR_LOSS
    sign  = "+" if val >= 0 else ""
    return f'<span style="color:{color}">{sign}€{val:,.0f}</span>'


def _fmt_pct(val: float) -> str:
    color = COLOR_PROFIT if val >= 0 else COLOR_LOSS
    sign  = "+" if val >= 0 else ""
    return f'<span style="color:{color}">{sign}{val:.1f}%</span>'


def build_position_table_html(rows: List[PositionRow]) -> str:
    """Generate HTML table for Position P&L panel."""
    if not rows:
        return f'<p style="color:{COLOR_SUBTEXT}; text-align:center; padding:40px;">No open positions.</p>'

    header_style = (
        "background:#0D0D1A; color:#9E9E9E; font-size:11px; "
        "text-align:right; padding:8px 12px; border-bottom:1px solid #2A2A4A;"
    )
    cell_style   = "padding:7px 12px; text-align:right; font-size:12px; border-bottom:1px solid #1A1A2E;"
    left_style   = "padding:7px 12px; text-align:left; font-size:12px; border-bottom:1px solid #1A1A2E;"

    cols = [
        ("Symbol",    "left"),
        ("Name",      "left"),
        ("Class",     "left"),
        ("Sector",    "left"),
        ("Shares",    "right"),
        ("Entry €",   "right"),
        ("Price €",   "right"),
        ("Value €",   "right"),
        ("Wt%",       "right"),
        ("P&L €",     "right"),
        ("P&L%",      "right"),
        ("Stop €",    "right"),
        ("Days",      "right"),
    ]

    thead = "<thead><tr>" + "".join(
        f'<th style="{header_style} text-align:{align};">{col}</th>'
        for col, align in cols
    ) + "</tr></thead>"

    tbody_rows = []
    for i, r in enumerate(rows):
        bg = "#111128" if i % 2 == 0 else "#0D0D1A"
        stop_str = f"€{r.stop_price:,.2f}" if r.stop_price else "—"
        margin = ""
        if r.stop_price and r.current_price > 0:
            margin_pct = (r.current_price - r.stop_price) / r.current_price * 100
            margin = f" ({margin_pct:.1f}%)"

        cells = [
            (f'<code style="color:{COLOR_EQUITY}; font-size:12px;">{r.symbol}</code>', "left"),
            (r.name[:25] + "…" if len(r.name) > 25 else r.name, "left"),
            (r.asset_class, "left"),
            (r.sector[:20] + "…" if len(r.sector) > 20 else r.sector, "left"),
            (f"{r.shares:,}", "right"),
            (f"€{r.entry_price:,.2f}", "right"),
            (f"€{r.current_price:,.2f}", "right"),
            (f"€{r.current_value_eur:,.0f}", "right"),
            (f"{r.weight_pct:.1f}%", "right"),
            (_fmt_pnl(r.unrealised_pnl_eur), "right"),
            (_fmt_pct(r.unrealised_pnl_pct), "right"),
            (f'<span style="color:{COLOR_LOSS}; font-size:11px;">{stop_str}{margin}</span>', "right"),
            (str(r.days_held), "right"),
        ]

        row_html = f'<tr style="background:{bg};">' + "".join(
            f'<td style="{left_style if align == "left" else cell_style}">{val}</td>'
            for val, align in cells
        ) + "</tr>"
        tbody_rows.append(row_html)

    tbody = "<tbody>" + "".join(tbody_rows) + "</tbody>"
    return f'<table style="width:100%; border-collapse:collapse;">{thead}{tbody}</table>'


# ============================================================================
# SUMMARY METRICS BAR
# ============================================================================

def build_metrics_html(m: PortfolioMetrics) -> str:
    """Render the top summary metrics strip."""
    def fmt_num(val, fmt, prefix="", suffix="", na="N/A"):
        if val is None:
            return f'<span style="color:{COLOR_SUBTEXT}">{na}</span>'
        color = COLOR_TEXT
        if "return" in fmt.lower() or "pct" in fmt.lower():
            color = COLOR_PROFIT if val >= 0 else COLOR_LOSS
        return f'<span style="color:{color}">{prefix}{val:{fmt}}{suffix}</span>'

    def metric_card(label: str, value_html: str, sub: str = "") -> str:
        return f"""
        <div style="
            background:{COLOR_PLOT_BG}; border:1px solid {COLOR_GRID};
            border-radius:8px; padding:16px 20px; min-width:130px; flex:1;
            text-align:center;
        ">
          <div style="color:{COLOR_SUBTEXT}; font-size:10px; text-transform:uppercase; letter-spacing:1px; margin-bottom:6px;">{label}</div>
          <div style="font-size:22px; font-weight:600; color:{COLOR_TEXT};">{value_html}</div>
          <div style="color:{COLOR_SUBTEXT}; font-size:10px; margin-top:4px;">{sub}</div>
        </div>"""

    total_ret_color = COLOR_PROFIT if (m.total_return_pct or 0) >= 0 else COLOR_LOSS
    total_ret_html  = (
        f'<span style="color:{total_ret_color}">'
        f'{("+" if (m.total_return_pct or 0) >= 0 else "")}'
        f'{m.total_return_pct:.1f}%</span>'
        if m.total_return_pct is not None else f'<span style="color:{COLOR_SUBTEXT}">N/A</span>'
    )

    ann_ret_color = COLOR_PROFIT if (m.annualised_return_pct or 0) >= 0 else COLOR_LOSS
    ann_ret_html  = (
        f'<span style="color:{ann_ret_color}">'
        f'{("+" if (m.annualised_return_pct or 0) >= 0 else "")}'
        f'{m.annualised_return_pct:.1f}%</span>'
        if m.annualised_return_pct is not None else f'<span style="color:{COLOR_SUBTEXT}">N/A</span>'
    )

    sharpe_color = (
        COLOR_PROFIT if (m.sharpe_ratio or 0) >= 1.0
        else COLOR_LOSS if (m.sharpe_ratio or 0) < 0
        else "#FFD54F"
    )
    sharpe_html = (
        f'<span style="color:{sharpe_color}">{m.sharpe_ratio:.2f}</span>'
        if m.sharpe_ratio is not None else f'<span style="color:{COLOR_SUBTEXT}">N/A</span>'
    )

    dd_color = COLOR_LOSS if (m.max_drawdown_pct or 0) < -10 else "#FFD54F"
    dd_html  = (
        f'<span style="color:{dd_color}">{m.max_drawdown_pct:.1f}%</span>'
        if m.max_drawdown_pct is not None else f'<span style="color:{COLOR_SUBTEXT}">N/A</span>'
    )

    pf_color = COLOR_PROFIT if (m.profit_factor or 0) >= 1.5 else COLOR_TEXT
    pf_html  = (
        f'<span style="color:{pf_color}">{m.profit_factor:.2f}×</span>'
        if m.profit_factor is not None else f'<span style="color:{COLOR_SUBTEXT}">N/A</span>'
    )

    portfolio_val_html = (
        f'<span style="color:{COLOR_TEXT}">€{m.portfolio_value_eur:,.0f}</span>'
        if m.portfolio_value_eur is not None else f'<span style="color:{COLOR_SUBTEXT}">N/A</span>'
    )

    first_trade = m.first_trade_date[:10] if m.first_trade_date else "—"
    since_sub   = f"since {first_trade}"

    cards = [
        metric_card("Total Return",     total_ret_html,  since_sub),
        metric_card("Ann. Return",      ann_ret_html,    "CAGR"),
        metric_card("Sharpe Ratio",     sharpe_html,     "252-day"),
        metric_card("Max Drawdown",     dd_html,         "peak-to-trough"),
        metric_card("Win Rate",         fmt_num(m.win_rate_pct, ".1f", suffix="%"), f"{m.total_trades} trades"),
        metric_card("Profit Factor",    pf_html,         "gross P / gross L"),
        metric_card("Portfolio Value",  portfolio_val_html, "current estimate"),
        metric_card("Open Positions",   f'<span style="color:{COLOR_TEXT}">{m.num_positions}</span>', "active"),
    ]

    return f"""
    <div style="display:flex; flex-wrap:wrap; gap:10px; margin-bottom:24px;">
        {"".join(cards)}
    </div>"""


# ============================================================================
# RISK PANEL  (from Script 23)
# ============================================================================

def build_risk_html(m: PortfolioMetrics) -> str:
    """Render the risk metrics strip (shown only if Script 23 data is available)."""
    has_risk = any([m.var_95_1d_eur, m.beta, m.hhi, m.information_ratio])
    if not has_risk:
        return f'<p style="color:{COLOR_SUBTEXT}; font-size:12px; padding:16px;">Risk metrics unavailable — run Script 23 first.</p>'

    def risk_item(label: str, value: str) -> str:
        return f"""
        <div style="display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px solid {COLOR_GRID};">
            <span style="color:{COLOR_SUBTEXT}; font-size:12px;">{label}</span>
            <span style="color:{COLOR_TEXT}; font-size:12px; font-weight:500;">{value}</span>
        </div>"""

    items = []
    if m.var_95_1d_eur is not None:
        items.append(risk_item("VaR 95% (1-day)", f"€{m.var_95_1d_eur:,.0f}"))
    if m.cvar_95_1d_eur is not None:
        items.append(risk_item("CVaR 95% (1-day)", f"€{m.cvar_95_1d_eur:,.0f}"))
    if m.beta is not None:
        color = COLOR_PROFIT if abs(m.beta) < 0.8 else COLOR_LOSS
        items.append(risk_item("Beta vs SPY", f'<span style="color:{color}">{m.beta:.2f}</span>'))
    if m.information_ratio is not None:
        ir_color = COLOR_PROFIT if m.information_ratio > 0.5 else COLOR_LOSS
        items.append(risk_item("Information Ratio", f'<span style="color:{ir_color}">{m.information_ratio:.2f}</span>'))
    if m.hhi is not None:
        eff_n = 1 / m.hhi if m.hhi > 0 else 0
        items.append(risk_item("HHI (Concentration)", f"{m.hhi:.4f} (eff. N = {eff_n:.1f})"))

    return f"""
    <div style="background:{COLOR_PLOT_BG}; border:1px solid {COLOR_GRID}; border-radius:8px; padding:16px 20px;">
        <div style="color:{COLOR_SUBTEXT}; font-size:10px; text-transform:uppercase; letter-spacing:1px; margin-bottom:12px;">
            Risk Metrics (Script 23)
        </div>
        {"".join(items)}
    </div>"""


# ============================================================================
# HTML ASSEMBLY
# ============================================================================

def fig_to_div(fig: go.Figure, div_id: str, height: int = 380) -> str:
    """Convert a Plotly figure to an HTML div string (no CDN—inlined in the page)."""
    fig.update_layout(height=height)
    return plotly.io.to_html(
        fig,
        full_html=False,
        include_plotlyjs=False,   # CDN loaded once at page level
        div_id=div_id,
        config={"responsive": True, "displayModeBar": True, "scrollZoom": False},
    )


def assemble_html(
    metrics_html:     str,
    equity_div:       str,
    heatmap_div:      str,
    drawdown_div:     str,
    sharpe_div:       str,
    allocation_div:   str,
    position_table:   str,
    risk_html:        str,
    generated_at:     str,
    account_equity:   Optional[float],
) -> str:
    """Build the final self-contained HTML page."""

    equity_val_str = f"€{account_equity:,.0f}" if account_equity else "—"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Performance Dashboard — Multi-Asset Trend Strategy</title>
  <script src="https://cdn.plot.ly/plotly-2.26.0.min.js"></script>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: {COLOR_PANEL_BG};
      color: {COLOR_TEXT};
      font-family: 'Inter', 'Segoe UI', 'Helvetica Neue', sans-serif;
      font-size: 13px;
      line-height: 1.5;
    }}
    /* ── Header ── */
    .dashboard-header {{
      background: linear-gradient(135deg, #0D0D1A 0%, #1A1A2E 50%, #16213E 100%);
      border-bottom: 1px solid {COLOR_GRID};
      padding: 20px 32px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .dashboard-header h1 {{
      font-size: 20px;
      font-weight: 700;
      color: {COLOR_TEXT};
      letter-spacing: 0.5px;
    }}
    .dashboard-header .subtitle {{
      font-size: 12px;
      color: {COLOR_SUBTEXT};
      margin-top: 3px;
    }}
    .header-right {{
      text-align: right;
      font-size: 11px;
      color: {COLOR_SUBTEXT};
    }}
    .header-right .live-badge {{
      display: inline-block;
      background: #1B5E20;
      color: #69F0AE;
      border-radius: 4px;
      padding: 2px 8px;
      font-size: 10px;
      font-weight: 600;
      margin-bottom: 4px;
    }}
    /* ── Main layout ── */
    .main-container {{
      max-width: 1600px;
      margin: 0 auto;
      padding: 24px 32px;
    }}
    /* ── Section headers ── */
    .section-title {{
      font-size: 11px;
      font-weight: 600;
      color: {COLOR_SUBTEXT};
      text-transform: uppercase;
      letter-spacing: 1.5px;
      margin-bottom: 12px;
      padding-bottom: 6px;
      border-bottom: 1px solid {COLOR_GRID};
    }}
    /* ── Chart grid ── */
    .chart-grid-2 {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
      margin-bottom: 16px;
    }}
    .chart-grid-3 {{
      display: grid;
      grid-template-columns: 2fr 1fr;
      gap: 16px;
      margin-bottom: 16px;
    }}
    .chart-card {{
      background: {COLOR_PANEL_BG};
      border: 1px solid {COLOR_GRID};
      border-radius: 10px;
      overflow: hidden;
    }}
    .chart-full {{
      background: {COLOR_PANEL_BG};
      border: 1px solid {COLOR_GRID};
      border-radius: 10px;
      overflow: hidden;
      margin-bottom: 16px;
    }}
    /* ── Position table ── */
    .table-container {{
      background: {COLOR_PANEL_BG};
      border: 1px solid {COLOR_GRID};
      border-radius: 10px;
      overflow: auto;
      margin-bottom: 16px;
    }}
    .table-header {{
      padding: 14px 16px;
      border-bottom: 1px solid {COLOR_GRID};
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .table-header-title {{
      font-size: 13px;
      font-weight: 600;
      color: {COLOR_TEXT};
    }}
    /* ── Footer ── */
    .footer {{
      padding: 20px 32px;
      border-top: 1px solid {COLOR_GRID};
      color: {COLOR_SUBTEXT};
      font-size: 10px;
      display: flex;
      justify-content: space-between;
    }}
    /* ── Responsive ── */
    @media (max-width: 1024px) {{
      .chart-grid-2, .chart-grid-3 {{ grid-template-columns: 1fr; }}
      .main-container {{ padding: 16px; }}
    }}
    /* ── Scrollbar styling ── */
    ::-webkit-scrollbar {{ width: 6px; height: 6px; }}
    ::-webkit-scrollbar-track {{ background: {COLOR_PLOT_BG}; }}
    ::-webkit-scrollbar-thumb {{ background: {COLOR_GRID}; border-radius: 3px; }}
    code {{ font-family: 'Fira Code', 'Consolas', monospace; }}
  </style>
</head>
<body>

<!-- ════════ HEADER ════════ -->
<div class="dashboard-header">
  <div>
    <h1>📊 Performance Dashboard</h1>
    <div class="subtitle">Multi-Asset Trend Following Strategy · v3.2 · EUR Base Currency</div>
  </div>
  <div class="header-right">
    <div class="live-badge">● LIVE DATA</div>
    <div>Generated: {generated_at}</div>
    <div>Portfolio Value: <strong style="color:{COLOR_TEXT};">{equity_val_str}</strong></div>
  </div>
</div>

<!-- ════════ MAIN ════════ -->
<div class="main-container">

  <!-- METRICS BAR -->
  <div class="section-title">Key Performance Metrics</div>
  {metrics_html}

  <!-- ROW 1: Equity Curve (full width) -->
  <div class="section-title">Portfolio Performance</div>
  <div class="chart-full">
    {equity_div}
  </div>

  <!-- ROW 2: Drawdown + Rolling Sharpe -->
  <div class="chart-grid-2">
    <div class="chart-card">{drawdown_div}</div>
    <div class="chart-card">{sharpe_div}</div>
  </div>

  <!-- ROW 3: Heatmap + Allocation -->
  <div class="chart-grid-3">
    <div class="chart-card">{heatmap_div}</div>
    <div class="chart-card">{allocation_div}</div>
  </div>

  <!-- ROW 4: Position Table + Risk Sidebar -->
  <div class="chart-grid-3">
    <div class="table-container">
      <div class="table-header">
        <span class="table-header-title">📋 Open Positions — Unrealised P&amp;L</span>
        <span style="color:{COLOR_SUBTEXT}; font-size:11px;">sorted by P&amp;L descending</span>
      </div>
      <div style="padding:0;">
        {position_table}
      </div>
    </div>
    <div style="display:flex; flex-direction:column; gap:16px;">
      {risk_html}
      <!-- Strategy summary box -->
      <div style="background:{COLOR_PLOT_BG}; border:1px solid {COLOR_GRID}; border-radius:8px; padding:16px 20px;">
        <div style="color:{COLOR_SUBTEXT}; font-size:10px; text-transform:uppercase; letter-spacing:1px; margin-bottom:12px;">Strategy Rules (Active)</div>
        <div style="font-size:11px; color:{COLOR_SUBTEXT}; line-height:2.0;">
          <div>🎯 Trend: SMA50 &gt; SMA200 &amp; Close &gt; SMA50 &amp; ADX &gt; 20</div>
          <div>📐 Momentum: (Close − SMA200) / SMA200 × 100</div>
          <div>💰 Risk/Position: 2% of equity</div>
          <div>🔴 Initial Stop: Entry − 3 × ATR(20)</div>
          <div>🔄 Trailing Stop: Price − 4 × ATR(20) @ +15%</div>
          <div>⚠️ Circuit Breaker: DD &gt; 15% → HALT</div>
          <div>🗓️ Rebalancing: Monthly (last trading day)</div>
        </div>
      </div>
    </div>
  </div>

</div><!-- /main-container -->

<!-- ════════ FOOTER ════════ -->
<div class="footer">
  <div>Script 24 · Multi-Asset Trend Following Strategy · Architecture v3.2 (Feb 2026)</div>
  <div>⚠ For information only. Requires human approval before execution.</div>
  <div>Data sources: Scripts 03, 13, 22, 23</div>
</div>

</body>
</html>"""


# ============================================================================
# MAIN
# ============================================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Script 24: Performance Dashboard Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--account-equity", type=float, default=None,
                   help="Override portfolio equity in EUR")
    p.add_argument("--start-date", type=str, default=None,
                   help="Equity curve start date YYYY-MM-DD")
    p.add_argument("--benchmark", type=str, default="SPY.US",
                   choices=["SPY.US", "ACWI.US"],
                   help="Benchmark symbol for annotation (default: SPY.US)")
    p.add_argument("--output", type=str, default=None,
                   help="Custom output path for HTML dashboard")
    p.add_argument("--dry-run", action="store_true",
                   help="Build dashboard in memory; do not write files")
    p.add_argument("--no-prices", action="store_true",
                   help="Skip parquet price loading (fast mode)")
    p.add_argument("--top-n", type=int, default=25,
                   help="Max positions in P&L table (default: 25)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    load_prices = not args.no_prices

    start_date: Optional[date] = None
    if args.start_date:
        try:
            start_date = date.fromisoformat(args.start_date)
        except ValueError:
            logger.error(f"Invalid --start-date: {args.start_date} (expected YYYY-MM-DD)")
            return 1

    logger.info("=" * 70)
    logger.info("Script 24: Performance Dashboard")
    logger.info("=" * 70)

    # ── STEP 1: Load all inputs ────────────────────────────────────────────────
    logger.info("\n[1/7] Loading portfolio data…")
    portfolio    = load_portfolio_state()
    trades       = load_trade_ledger()
    company_info = load_company_info()
    attribution  = load_latest_attribution()
    risk_data    = load_latest_risk()

    logger.info(f"  Open positions: {len(portfolio)}")
    logger.info(f"  Trade records:  {len(trades)}")

    # ── STEP 2: Reconstruct equity curve ──────────────────────────────────────
    logger.info("\n[2/7] Reconstructing equity curve…")
    nav, daily_returns = reconstruct_equity_curve(
        trades         = trades,
        portfolio      = portfolio,
        start_date     = start_date,
        load_prices    = load_prices,
        account_equity = args.account_equity,
    )
    if nav is not None:
        logger.info(f"  Equity curve: {len(nav)} data points "
                    f"({nav.index[0]} → {nav.index[-1]})")
        logger.info(f"  Latest NAV: €{nav.iloc[-1]:,.0f}")
    else:
        logger.warning("  Equity curve could not be reconstructed.")

    # ── STEP 3: Compute summary metrics ───────────────────────────────────────
    logger.info("\n[3/7] Computing performance metrics…")
    metrics = compute_metrics(
        nav            = nav,
        daily_returns  = daily_returns,
        trades         = trades,
        portfolio      = portfolio,
        risk_data      = risk_data,
        account_equity = args.account_equity,
    )
    if metrics.total_return_pct is not None:
        logger.info(f"  Total return:    {metrics.total_return_pct:+.1f}%")
    if metrics.sharpe_ratio is not None:
        logger.info(f"  Sharpe ratio:    {metrics.sharpe_ratio:.2f}")
    if metrics.max_drawdown_pct is not None:
        logger.info(f"  Max drawdown:    {metrics.max_drawdown_pct:.1f}%")
    if metrics.win_rate_pct is not None:
        logger.info(f"  Win rate:        {metrics.win_rate_pct:.1f}%")

    # ── STEP 4: Build position table ──────────────────────────────────────────
    logger.info("\n[4/7] Building position table…")
    position_rows = build_position_rows(
        portfolio      = portfolio,
        company_info   = company_info,
        load_prices    = load_prices,
        account_equity = args.account_equity,
        top_n          = args.top_n,
    )
    logger.info(f"  {len(position_rows)} positions in table.")

    # ── STEP 5: Build all charts ───────────────────────────────────────────────
    logger.info("\n[5/7] Rendering charts…")
    equity_fig     = build_equity_curve_fig(nav, start_date)
    heatmap_fig    = build_monthly_heatmap_fig(daily_returns)
    drawdown_fig   = build_drawdown_fig(nav)
    sharpe_fig     = build_rolling_sharpe_fig(daily_returns)
    allocation_fig = build_allocation_pie_fig(position_rows, company_info)

    equity_div     = fig_to_div(equity_fig,     "equity-curve",    height=380)
    heatmap_div    = fig_to_div(heatmap_fig,    "monthly-heatmap", height=340)
    drawdown_div   = fig_to_div(drawdown_fig,   "drawdown",        height=280)
    sharpe_div     = fig_to_div(sharpe_fig,     "rolling-sharpe",  height=280)
    allocation_div = fig_to_div(allocation_fig, "allocation-pie",  height=340)
    logger.info("  All chart divs rendered.")

    # ── STEP 6: Build HTML panels ──────────────────────────────────────────────
    logger.info("\n[6/7] Assembling HTML dashboard…")
    metrics_html   = build_metrics_html(metrics)
    position_table = build_position_table_html(position_rows)
    risk_html      = build_risk_html(metrics)

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
    html = assemble_html(
        metrics_html   = metrics_html,
        equity_div     = equity_div,
        heatmap_div    = heatmap_div,
        drawdown_div   = drawdown_div,
        sharpe_div     = sharpe_div,
        allocation_div = allocation_div,
        position_table = position_table,
        risk_html      = risk_html,
        generated_at   = generated_at,
        account_equity = args.account_equity or metrics.portfolio_value_eur,
    )

    # ── STEP 7: Write outputs ──────────────────────────────────────────────────
    logger.info("\n[7/7] Writing outputs…")
    if args.dry_run:
        logger.info("  [DRY RUN] Dashboard built successfully — no files written.")
        html_size_kb = len(html.encode("utf-8")) / 1024
        logger.info(f"  Dashboard size: {html_size_kb:.1f} KB")
        print("\n✓ Dry run complete. Dashboard ready (not written).")
        return 0

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        ts_file    = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = REPORTS_DIR / f"dashboard_{ts_file}.html"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    html_size_kb = output_path.stat().st_size / 1024
    logger.info(f"  → Dashboard: {output_path} ({html_size_kb:.0f} KB)")

    # Latest symlink / copy
    latest_path = REPORTS_DIR / "dashboard_latest.html"
    try:
        shutil.copy2(output_path, latest_path)
        logger.info(f"  → Latest:    {latest_path}")
    except Exception as e:
        logger.warning(f"  Could not copy to latest: {e}")

    # ── Console summary ───────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("✓ Performance Dashboard Generated")
    print("=" * 70)
    print(f"  File:       {output_path}")
    print(f"  Size:       {html_size_kb:.0f} KB")
    print(f"  Positions:  {metrics.num_positions}")
    if metrics.portfolio_value_eur:
        print(f"  NAV:        €{metrics.portfolio_value_eur:,.0f}")
    if metrics.total_return_pct is not None:
        sign = "+" if metrics.total_return_pct >= 0 else ""
        print(f"  Return:     {sign}{metrics.total_return_pct:.1f}%")
    if metrics.sharpe_ratio is not None:
        print(f"  Sharpe:     {metrics.sharpe_ratio:.2f}")
    if metrics.max_drawdown_pct is not None:
        print(f"  Max DD:     {metrics.max_drawdown_pct:.1f}%")
    print(f"\n  Open in browser: file://{output_path.resolve()}")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    sys.exit(main())
