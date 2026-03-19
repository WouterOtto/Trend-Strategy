#!/usr/bin/env python3
"""
Script 15: Technical Analysis Chart Generator
==============================================
Generate an interactive single-file HTML dashboard with price, SMA, volume,
ADX, and ATR charts for selected symbols.

Purpose:
    Provide a visual review layer on top of the systematic pipeline outputs.
    Every chart is reproducible from the on-disk indicator cache – no live data
    required.  Designed as a read-only inspection tool: no data is modified.

Dependencies:
    - Script 5:  Indicator Calculator   (data_cache/indicators/*.parquet)
    - Script 6:  Trend Qualifier        (data_cache/signals/qualified_trends.json)
    - Script 7:  Momentum Ranker        (data_cache/signals/momentum_ranked.json)
    - Script 4:  Universe Screener      (data_cache/qualified/qualified_symbols.json)

Optional inputs (enrichment only, script runs without them):
    - Script 11: Monthly Rebalancer     (reports/rebalancing/*_recommendations.json)
    - data_cache/portfolio/position_sizes.json
    - data_cache/portfolio/stop_levels.json
    - data/portfolio_state.json         (current holdings)

Outputs:
    - reports/charts/technical_analysis_{YYYYMMDD_HHMMSS}.html

Chart Layout (per symbol – 4 stacked rows):
    Row 1 (60%): Adjusted-close price  + SMA 50  + SMA 200
    Row 2 (20%): Volume bars
    Row 3 (10%): ADX (14) with threshold line at 20
    Row 4 (10%): ATR % (20)

Execution:
    # Qualified universe with scenario filters (default)
    python scripts/15_generate_technical_charts.py

    # Specific symbols
    python scripts/15_generate_technical_charts.py --symbols AAPL.US,MSFT.US,NVDA.US

    # Top 20 by momentum only
    python scripts/15_generate_technical_charts.py --filter top

    # All qualified-trend symbols
    python scripts/15_generate_technical_charts.py --filter qualified

    # All screened symbols
    python scripts/15_generate_technical_charts.py --filter all

    # Custom lookback (trading days)
    python scripts/15_generate_technical_charts.py --lookback 300

    # Save to custom path
    python scripts/15_generate_technical_charts.py --output /tmp/my_charts.html

Architecture: v3.2 (Feb 2026)
"""

import sys
import json
import logging
import argparse
import warnings
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd
import numpy as np

# Optional Plotly – fail with a helpful message if not installed
try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    import plotly
except ImportError:
    print("[ERROR] plotly is required: pip install plotly")
    sys.exit(1)

warnings.filterwarnings('ignore', category=pd.errors.PerformanceWarning)

# ============================================================================
# PATHS
# ============================================================================

PROJECT_ROOT     = Path(__file__).parent.parent
DATA_CACHE_DIR   = PROJECT_ROOT / "data_cache"
INDICATORS_DIR   = DATA_CACHE_DIR / "indicators"
SIGNALS_DIR      = DATA_CACHE_DIR / "signals"
QUALIFIED_DIR    = DATA_CACHE_DIR / "qualified"
PORTFOLIO_DIR    = DATA_CACHE_DIR / "portfolio"
FUNDAMENTALS_DIR = PROJECT_ROOT.parent / "data_load" / "data_cache" / "fundamentals"
REPORTS_DIR      = PROJECT_ROOT / "reports" / "charts"
LOG_DIR          = PROJECT_ROOT / "logs"

# ============================================================================
# CONFIGURATION CONSTANTS
# ============================================================================

DEFAULT_LOOKBACK    = 200       # Trading days displayed per chart
DEFAULT_TOP_N       = 20        # Symbols when --filter top (default)
ADX_TREND_THRESHOLD = 20        # Threshold line drawn on ADX panel
CHART_HEIGHT_PX     = 900       # Total chart height per symbol (pixels)
PLOTLY_CDN          = "https://cdn.plot.ly/plotly-2.32.0.min.js"

# ============================================================================
# COLOUR PALETTE  (consistent across all charts)
# ============================================================================

COLOURS = {
    "price":        "#2563EB",   # Blue 600
    "sma_50":       "#F59E0B",   # Amber 400
    "sma_200":      "#EF4444",   # Red 500
    "volume":       "#94A3B8",   # Slate 400
    "adx":          "#8B5CF6",   # Violet 500
    "adx_fill":     "rgba(139,92,246,0.15)",
    "atr":          "#F97316",   # Orange 500
    "atr_fill":     "rgba(249,115,22,0.15)",
    "stop":         "#DC2626",   # Red 600
    "bg":           "#F8FAFC",   # Slate 50
    "sidebar_bg":   "#1E293B",   # Slate 800
    "sidebar_hover":"#334155",   # Slate 700
    "header_bg":    "#0F172A",   # Slate 900
    "accent":       "#10B981",   # Emerald 500
    "positive":     "#10B981",
    "negative":     "#EF4444",
    "neutral":      "#94A3B8",
}

# Global variable to pass scenario tags from resolve_symbols() to main()
# when --filter recommendations is used. Populated by resolve_symbols(),
# consumed by main() to annotate metadata entries.
_SCENARIO_TAGS: Dict[str, List[str]] = {}

# ============================================================================
# HELPERS
# ============================================================================

def _fmt_market_cap(value) -> str:
    """
    Format a raw market-cap number into a compact string.

    Examples:
        3_200_000_000_000  →  "$3.20T"
          450_000_000_000  →  "$450B"
           12_000_000_000  →  "$12.0B"
              500_000_000  →  "$500M"
                  None / 0  →  "–"
    """
    if value is None:
        return "–"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "–"
    if v <= 0:
        return "–"
    if v >= 1e12:
        return f"${v/1e12:.2f}T"
    if v >= 1e9:
        return f"${v/1e9:.1f}B"
    if v >= 1e6:
        return f"${v/1e6:.0f}M"
    return f"${v:,.0f}"


def _normalise_asset_type(raw: str) -> str:
    """
    Normalise asset-class strings from various upstream sources to one of:
        "stock" | "etf" | "crypto" | "other"
    """
    if not raw:
        return "stock"   # sensible default for equity universe
    r = raw.strip().lower()
    if r in ("etf", "fund", "mutual fund", "exchange traded fund"):
        return "etf"
    if r in ("crypto", "cryptocurrency", "digital asset", "coin"):
        return "crypto"
    if r in ("stock", "equity", "common stock", "ordinary shares", "share"):
        return "stock"
    # Partial matches
    if "etf" in r or "fund" in r:
        return "etf"
    if "crypto" in r or "coin" in r:
        return "crypto"
    return "stock"


# ============================================================================
# LOGGING
# ============================================================================

def _setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts  = datetime.now().strftime('%Y%m%d_%H%M%S')
    fh  = logging.FileHandler(LOG_DIR / f"generate_charts_{ts}.log", encoding="utf-8")
    sh  = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    fh.setFormatter(fmt)
    sh.setFormatter(fmt)
    logger = logging.getLogger("chart_generator")
    logger.setLevel(logging.INFO)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger

logger = _setup_logging()

# ============================================================================
# DATA LOADING HELPERS
# ============================================================================

def _load_json(path: Path) -> Optional[object]:
    """Load a JSON file; return None with a warning if missing."""
    if not path.exists():
        logger.warning(f"File not found (optional): {path}")
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Could not parse {path}: {e}")
        return None


def load_metadata() -> Dict[str, Dict]:
    """
    Build a symbol → metadata dict from qualified_symbols.json.

    Script 4 saves as a list of dicts:
        [{"symbol": "AAPL.US", "name": "Apple Inc.", ...}, ...]

    Returns:
        { "AAPL.US": {"name": "Apple Inc.", "sector": "Technology", ...}, ... }
    """
    raw = _load_json(QUALIFIED_DIR / "qualified_symbols.json")
    if not raw:
        return {}

    meta: Dict[str, Dict] = {}
    entries = raw if isinstance(raw, list) else list(raw.values())
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        sym = entry.get("symbol")
        if not sym:
            continue
        meta[sym] = {
            "name":       entry.get("name", sym),
            "sector":     entry.get("sector", "Unknown"),
            "exchange":   entry.get("exchange", ""),
            "market_cap": entry.get("market_cap", None),
            "asset_type": _normalise_asset_type(
                entry.get("asset_class") or entry.get("type") or entry.get("asset_type") or ""
            ),
            # Keep base_ticker so build_base_ticker_index() can read it
            "_base_ticker": entry.get("base_ticker") or sym.rsplit(".", 1)[0],
        }
    return meta


def build_base_ticker_index(metadata: Dict[str, Dict]) -> Dict[str, str]:
    """
    Build a reverse lookup: bare_ticker → full_EODHD_symbol.

    company_info.json (Script 2) is keyed by bare ticker codes
    (e.g. "AAPL", "0P0001O70F") because Script 2 reads them from the
    raw_bulk parquet "code" column, which has no exchange suffix.

    qualified_symbols.json (Script 4) is keyed by full EODHD symbols
    (e.g. "AAPL.US").

    This index bridges the two so that market_cap from company_info.json
    can be merged into the metadata dict.

    Returns:
        { "AAPL": "AAPL.US", "MSFT": "MSFT.US", ... }
    """
    index: Dict[str, str] = {}
    for full_sym, info in metadata.items():
        base = info.get("_base_ticker") or full_sym.rsplit(".", 1)[0]
        if base:
            index[base] = full_sym
    return index


def load_momentum_ranked() -> List[Dict]:
    """
    Return the full ranked list from momentum_ranked.json (Script 7 output).

    Script 7 saves as a nested dict:
        {
            "metadata": { "as_of_date": ..., "total_ranked": ... },
            "ranked":   [ {"symbol": "AAPL.US", "momentum_score": 18.5, ...}, ... ]
        }
    """
    raw = _load_json(SIGNALS_DIR / "momentum_ranked.json")
    if raw is None:
        return []

    # Expected format: {"metadata": {...}, "ranked": [...]}
    if isinstance(raw, dict) and "ranked" in raw:
        ranked = raw["ranked"]
        return ranked if isinstance(ranked, list) else []

    # Defensive: plain list fallback
    if isinstance(raw, list):
        return raw

    return []


def load_qualified_trends() -> List[str]:
    """
    Return symbol list from qualified_trends.json (Script 6 output).

    Script 6 saves as a nested dict:
        {
            "as_of_date": "2026-01-31",
            "count": 312,
            "symbols": {
                "AAPL.US": {"symbol": "AAPL.US", "qualified": true, ...},
                ...
            }
        }
    """
    raw = _load_json(SIGNALS_DIR / "qualified_trends.json")
    if raw is None:
        return []

    # Expected format: {"symbols": {"AAPL.US": {...}, ...}}
    if isinstance(raw, dict) and "symbols" in raw:
        syms = raw["symbols"]
        if isinstance(syms, dict):
            return list(syms.keys())
        if isinstance(syms, list):
            return [s["symbol"] for s in syms if isinstance(s, dict) and "symbol" in s]

    # Defensive: plain list fallback
    if isinstance(raw, list):
        if raw and isinstance(raw[0], dict):
            return [r["symbol"] for r in raw if "symbol" in r]
        if raw and isinstance(raw[0], str):
            return raw

    return []


def load_position_sizes() -> Dict[str, float]:
    """
    Return symbol → position_value dict (optional, for sidebar enrichment).

    Script 8 saves as:
        {
            "metadata": {...},
            "positions": {
                "AAPL.US": {"symbol": ..., "position_value_eur": 15000.0, ...},
                ...
            }
        }
    """
    raw = _load_json(PORTFOLIO_DIR / "position_sizes.json")
    if raw is None:
        return {}

    # Expected format: {"positions": {"AAPL.US": {...}}}
    if isinstance(raw, dict) and "positions" in raw:
        positions = raw["positions"]
        if isinstance(positions, dict):
            result = {}
            for sym, pos in positions.items():
                if isinstance(pos, dict):
                    # Try common value keys in order of preference
                    val = (
                        pos.get("position_value_eur")
                        or pos.get("position_value_usd")
                        or pos.get("position_value")
                        or pos.get("dollar_allocation")
                        or pos.get("notional_value")
                    )
                    if val is not None:
                        try:
                            result[sym] = float(val)
                        except (TypeError, ValueError):
                            pass
            return result

    # Defensive: flat dict {symbol: value}
    if isinstance(raw, dict):
        result = {}
        for k, v in raw.items():
            if k == "metadata":
                continue
            if isinstance(v, (int, float)):
                result[k] = float(v)
            elif isinstance(v, dict):
                val = (
                    v.get("position_value_eur")
                    or v.get("position_value_usd")
                    or v.get("position_value")
                    or v.get("dollar_allocation")
                )
                if val is not None:
                    try:
                        result[k] = float(val)
                    except (TypeError, ValueError):
                        pass
        return result

    # Defensive: list of dicts
    if isinstance(raw, list):
        result = {}
        for r in raw:
            if isinstance(r, dict) and "symbol" in r:
                val = (
                    r.get("position_value_eur")
                    or r.get("position_value_usd")
                    or r.get("position_value")
                    or r.get("dollar_allocation")
                )
                if val is not None:
                    try:
                        result[r["symbol"]] = float(val)
                    except (TypeError, ValueError):
                        pass
        return result

    return {}


def load_stop_levels() -> Dict[str, float]:
    """
    Return symbol → stop_price dict (optional, drawn as horizontal line on chart).

    Script 8 saves as:
        {
            "metadata": {...},
            "stops": {
                "AAPL.US": {"stop_price": 205.50, "trailing_stop_price": ..., ...},
                ...
            }
        }
    """
    raw = _load_json(PORTFOLIO_DIR / "stop_levels.json")
    if raw is None:
        return {}

    # Expected format: {"stops": {"AAPL.US": {"stop_price": ..., ...}}}
    if isinstance(raw, dict) and "stops" in raw:
        stops = raw["stops"]
        if isinstance(stops, dict):
            result = {}
            for sym, stop_data in stops.items():
                if isinstance(stop_data, dict):
                    # Prefer trailing stop if active, fall back to initial stop
                    price = (
                        stop_data.get("trailing_stop_price")
                        or stop_data.get("stop_price")
                        or stop_data.get("initial_stop_price")
                    )
                    if price is not None:
                        try:
                            result[sym] = float(price)
                        except (TypeError, ValueError):
                            pass
            return result

    # Defensive: flat dict
    if isinstance(raw, dict):
        result = {}
        for k, v in raw.items():
            if k == "metadata":
                continue
            if isinstance(v, (int, float)):
                result[k] = float(v)
            elif isinstance(v, dict):
                price = (
                    v.get("trailing_stop_price")
                    or v.get("stop_price")
                    or v.get("initial_stop_price")
                )
                if price is not None:
                    try:
                        result[k] = float(price)
                    except (TypeError, ValueError):
                        pass
        return result

    return {}


def load_rebalancing_recommendations() -> Tuple[Dict[str, List[str]], Set[str]]:
    """
    Load the most recent monthly rebalancing recommendations from Script 11.

    Script 11 saves reports/rebalancing/{YYYY-MM}_recommendations.json with:
        {
            "scenario_1_pure_momentum": {
                "entries": {"new": [{"symbol": "AAPL.US", ...}, ...]},
                "holds": {"positions": [{"symbol": "MSFT.US", ...}, ...]}
            },
            "scenario_2_force_diversity": {...},
            "scenario_3_balanced": {...}
        }

    Returns:
        (symbol_sources, portfolio_symbols)
        - symbol_sources: {"AAPL.US": ["scenario1", "scenario3"], ...}
          Maps each recommended symbol to the list of scenarios that recommend it.
        - portfolio_symbols: set of symbols in current portfolio (from portfolio_state.json)
    """
    REBALANCING_DIR = PROJECT_ROOT / "reports" / "rebalancing"

    # Find most recent recommendations file
    if not REBALANCING_DIR.exists():
        logger.warning(f"Rebalancing directory not found: {REBALANCING_DIR}")
        return {}, set()

    rec_files = list(REBALANCING_DIR.glob("*_recommendations.json"))
    if not rec_files:
        logger.warning(f"No recommendations.json files found in {REBALANCING_DIR}")
        return {}, set()

    # Most recent file (lexicographically sorted: YYYY-MM_recommendations.json)
    #latest_file = sorted(rec_files)[-1]
    # Most recently *generated* file (by filesystem modification time).
    # Sorting by filename (YYYY-MM) picks the most recent calendar month,
    # which is wrong when Script 11 is re-run for an earlier period — the
    # re-run produces the freshest data but has an older month in its name.
    latest_file = max(rec_files, key=lambda p: p.stat().st_mtime)
    logger.info(f"  Loading: {latest_file.name}")

    raw = _load_json(latest_file)
    if not raw:
        return {}, set()

    # Track which scenarios recommend each symbol
    symbol_sources: Dict[str, List[str]] = {}
    
    scenarios = [
        ("scenario_1_pure_momentum", "scenario1"),
        ("scenario_2_force_diversity", "scenario2"),
        ("scenario_3_balanced", "scenario3"),
    ]

    for scenario_key, label in scenarios:
        scenario = raw.get(scenario_key, {})

        # New entries
        new_entries = scenario.get("entries", {}).get("new", [])
        for entry in new_entries:
            if isinstance(entry, dict) and "symbol" in entry:
                sym = entry["symbol"]
                if sym not in symbol_sources:
                    symbol_sources[sym] = []
                if label not in symbol_sources[sym]:
                    symbol_sources[sym].append(label)

        # Holds (existing positions to keep)
        holds = scenario.get("holds", {}).get("positions", [])
        for hold in holds:
            if isinstance(hold, dict) and "symbol" in hold:
                sym = hold["symbol"]
                if sym not in symbol_sources:
                    symbol_sources[sym] = []
                if label not in symbol_sources[sym]:
                    symbol_sources[sym].append(label)

    # Load current portfolio (if exists)
    portfolio_file = PROJECT_ROOT / "data" / "portfolio_state.json"
    portfolio_symbols = set()
    if portfolio_file.exists():
        logger.info(f"  Loading portfolio: {portfolio_file.name}")
        port_raw = _load_json(portfolio_file)
        if isinstance(port_raw, dict):
            # Check if it has a "positions" key (structured format)
            positions = port_raw.get("positions")
            
            if positions is not None:
                # Structured format: {"positions": {...}}
                if isinstance(positions, dict):
                    portfolio_symbols.update(positions.keys())
                    logger.info(f"  Portfolio format: structured dict with {len(positions)} positions")
                elif isinstance(positions, list):
                    for pos in positions:
                        if isinstance(pos, dict) and "symbol" in pos:
                            portfolio_symbols.add(pos["symbol"])
                    logger.info(f"  Portfolio format: structured list with {len(positions)} positions")
            else:
                # Flat format: {"AAPL.US": {...}, "MSFT.US": {...}}
                # Symbols are top-level keys directly
                portfolio_symbols.update(port_raw.keys())
                logger.info(f"  Portfolio format: flat dict with {len(port_raw)} positions (top-level keys)")
        else:
            logger.warning(f"  Portfolio file exists but has unexpected format: {type(port_raw)}")
    else:
        logger.info(f"  Portfolio file not found: {portfolio_file}")

    return symbol_sources, portfolio_symbols


def load_company_info() -> Dict[str, Dict]:
    """
    Load raw company info written by Script 2 (Yahoo Finance fundamentals).

    File: data_cache/fundamentals/company_info.json
    Format: { "AAPL.US": {"marketCap": 3.2e12, "sector": "Technology", ...}, ... }

    Returns symbol → raw Yahoo dict, or empty dict if file is missing.
    """
    raw = _load_json(FUNDAMENTALS_DIR / "company_info.json")
    if isinstance(raw, dict):
        return raw
    return {}



    """
    Fallback: discover symbols directly from indicator parquet files.

    Used when Scripts 6/7 have not been run yet.
    File naming convention (Script 5): {symbol}_indicators.parquet
    """
    files = sorted(INDICATORS_DIR.glob("*_indicators.parquet"))
    symbols = []
    for f in files:
        # Strip '_indicators' suffix to recover the original symbol string.
        # e.g. "AAPL.US_indicators" → "AAPL.US"
        # Using removesuffix (Python 3.9+) is safer than str.replace() which
        # would clobber a hypothetical '_indicators' substring inside a symbol.
        sym = f.stem.removesuffix("_indicators")
        symbols.append(sym)
    return symbols


def load_indicator_df(symbol: str) -> Optional[pd.DataFrame]:
    """
    Load indicator parquet for one symbol.

    Expected columns (from Script 5):
        open, high, low, close, adjusted_close, volume,
        sma_50, sma_200, atr_20_pct, adx_14

    Returns None if the file doesn't exist or has too few rows.
    """
    fpath = INDICATORS_DIR / f"{symbol}_indicators.parquet"
    if not fpath.exists():
        logger.warning(f"Indicator file missing: {fpath}")
        return None

    try:
        df = pd.read_parquet(fpath)

        # Ensure DatetimeIndex
        if not isinstance(df.index, pd.DatetimeIndex):
            if "date" in df.columns:
                df = df.set_index("date")
            df.index = pd.to_datetime(df.index)

        df.sort_index(inplace=True)

        # Cast numeric columns (guard against object dtype in parquet)
        numeric_cols = [
            "open", "high", "low", "close", "adjusted_close",
            "volume", "sma_50", "sma_200", "atr_20_pct", "adx_14",
        ]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        return df

    except Exception as e:
        logger.warning(f"Failed to load indicator data for {symbol}: {e}")
        return None

# ============================================================================
# SYMBOL SELECTION
# ============================================================================

def resolve_symbols(
    cli_symbols: Optional[str],
    filter_mode: str,
    top_n: int,
) -> List[str]:
    """
    Determine the list of symbols to chart based on CLI arguments.

    Priority:
        1. --symbols           → explicit override (always wins)
        2. --filter recommendations → Script 11/12 recommendations + current portfolio
        3. --filter qualified  → qualified_trends.json (Script 6 output)
        4. --filter all        → all symbols in qualified_symbols.json (Script 4 output)
        5. default (top)       → top N by momentum_ranked.json (Script 7 output)

    Fallback chain when signal files are not yet generated:
        momentum_ranked.json  →  qualified_trends.json
        qualified_trends.json →  qualified_symbols.json
        qualified_symbols.json → indicator cache directory (Scripts 1–5 only)
    """
    if cli_symbols:
        symbols = [s.strip().upper() for s in cli_symbols.split(",") if s.strip()]
        logger.info(f"Using {len(symbols)} explicitly specified symbols")
        return symbols

    if filter_mode == "recommendations":
        # Load ALL qualified symbols (tags already loaded globally in main())
        meta = load_metadata()
        symbols = list(meta.keys())
        
        if not symbols:
            logger.warning(
                "No qualified symbols found – falling back to indicator cache"
            )
            symbols = list_symbols_from_indicator_cache()
        
        if not symbols:
            logger.warning(
                "No symbols available – falling back to top N momentum"
            )
            filter_mode = "top"
        else:
            n_tagged = len(_SCENARIO_TAGS)
            logger.info(f"Recommendation filter:")
            logger.info(f"  - Total symbols       : {len(symbols)} (full qualified universe)")
            logger.info(f"  - Tagged with filters : {n_tagged} symbols")
            logger.info(f"  - Context only        : {len(symbols) - n_tagged} symbols (not in any scenario)")
            
            return symbols

    if filter_mode == "qualified":
        symbols = load_qualified_trends()
        if not symbols:
            logger.warning(
                "qualified_trends.json empty or missing – "
                "falling back to qualified_symbols.json"
            )
            meta = load_metadata()
            symbols = list(meta.keys())
        if not symbols:
            logger.warning(
                "qualified_symbols.json empty or missing – "
                "falling back to indicator cache"
            )
            symbols = list_symbols_from_indicator_cache()
        logger.info(f"Using {len(symbols)} qualified-trend symbols")
        return symbols

    if filter_mode == "all":
        symbols = list(load_metadata().keys())
        if not symbols:
            logger.warning(
                "qualified_symbols.json empty or missing – "
                "falling back to indicator cache"
            )
            symbols = list_symbols_from_indicator_cache()
        logger.info(f"Using {len(symbols)} screened symbols")
        return symbols

    # ââ Default: top N by momentum ââââââââââââââââââââââââââââââââââââââââ
    ranked = load_momentum_ranked()
    if ranked:
        symbols = [r["symbol"] for r in ranked[:top_n] if "symbol" in r]
        logger.info(f"Using top {len(symbols)} symbols by momentum score")
        return symbols

    logger.warning("momentum_ranked.json missing – falling back to qualified_trends.json")
    symbols = load_qualified_trends()
    if symbols:
        symbols = symbols[:top_n]
        logger.info(f"Using {len(symbols)} symbols from qualified_trends.json (capped at {top_n})")
        return symbols

    logger.warning("qualified_trends.json missing – falling back to qualified_symbols.json")
    symbols = list(load_metadata().keys())
    if symbols:
        symbols = symbols[:top_n]
        logger.info(f"Using {len(symbols)} symbols from qualified_symbols.json (capped at {top_n})")
        return symbols

    logger.warning(
        "qualified_symbols.json missing – falling back to indicator cache "
        f"(top {top_n} alphabetically)"
    )
    symbols = list_symbols_from_indicator_cache()[:top_n]
    logger.info(f"Using {len(symbols)} symbols from indicator cache")
    return symbols

# ============================================================================
# CHART BUILDER
# ============================================================================

class TechnicalChartGenerator:
    """
    Builds per-symbol Plotly figures and assembles the HTML dashboard.
    """

    def __init__(
        self,
        lookback_days: int = DEFAULT_LOOKBACK,
        metadata:      Optional[Dict[str, Dict]] = None,
        momentum_map:  Optional[Dict[str, float]] = None,
        position_map:  Optional[Dict[str, float]] = None,
        stop_map:      Optional[Dict[str, float]] = None,
        qualified_set: Optional[set] = None,
        show_recommendation_filters: bool = False,
    ):
        self.lookback_days = lookback_days
        self.metadata      = metadata      or {}
        self.momentum_map  = momentum_map  or {}
        self.position_map  = position_map  or {}
        self.stop_map      = stop_map      or {}
        self.qualified_set = qualified_set or set()
        self.show_recommendation_filters = show_recommendation_filters
        self._cache: Dict[str, pd.DataFrame] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_df(self, symbol: str) -> Optional[pd.DataFrame]:
        """Load (with in-memory cache) and trim to lookback window."""
        if symbol not in self._cache:
            df = load_indicator_df(symbol)
            if df is None:
                return None
            self._cache[symbol] = df

        df = self._cache[symbol].copy()

        # Trim to lookback (most recent N trading days)
        if len(df) > self.lookback_days:
            df = df.tail(self.lookback_days)

        # Determine price series to display
        # Prefer adjusted_close; fall back to close
        if "adjusted_close" in df.columns and df["adjusted_close"].notna().sum() > 10:
            df["_price"] = df["adjusted_close"]
        elif "close" in df.columns:
            df["_price"] = df["close"]
        else:
            logger.warning(f"{symbol}: No price column found")
            return None

        return df

    def _meta(self, symbol: str) -> Dict:
        return self.metadata.get(symbol, {
            "name": symbol, "sector": "–", "exchange": "",
            "asset_type": "stock", "market_cap": None,
        })

    # ------------------------------------------------------------------
    # Per-symbol Plotly figure
    # ------------------------------------------------------------------

    def build_figure(self, symbol: str) -> Optional[go.Figure]:
        """
        Build a 4-row Plotly figure for one symbol.

        Returns None if data is unavailable or insufficient.
        """
        df = self._get_df(symbol)
        if df is None or len(df) < 5:
            logger.warning(f"Skipping {symbol}: insufficient data")
            return None

        meta          = self._meta(symbol)
        name          = meta.get("name", symbol)
        sector        = meta.get("sector", "–")
        exchange      = meta.get("exchange", "")
        market_cap    = meta.get("market_cap", None)
        asset_type    = meta.get("asset_type", "stock")
        mom_score     = self.momentum_map.get(symbol)
        position_usd  = self.position_map.get(symbol)
        stop_price    = self.stop_map.get(symbol)
        is_qualified  = symbol in self.qualified_set

        # ââ Chart title (two lines via <br>, lives in top margin â cannot overlap) ââ
        cap_str  = _fmt_market_cap(market_cap)
        mom_str  = f"Momentum {mom_score:+.1f}%" if mom_score is not None else ""
        pos_str  = f"Position ${position_usd:,.0f}" if position_usd else ""
        badge    = "â QUALIFIED" if is_qualified else "â¬ UNQUALIFIED"
        parts    = [p for p in [exchange, sector, cap_str, mom_str, pos_str, badge]
                    if p and p not in ("–", "")]
        subtitle = "  Â·  ".join(parts)

        title_text = (
            f"<b>{name}</b>  ({symbol})"
            f"<br><span style='font-size:11px'>{subtitle}</span>"
        )

        # ââ Row height geometry â compute subplot vertical centres in paper coords ââ
        #
        # These constants must match the make_subplots() call below exactly.
        # Formula (Plotly, bottom=0, top=1):
        #   available = 1 - (n_rows-1) * vertical_spacing
        #   h_i       = row_heights[i] * available   (proportional allocation)
        #   Rows build upward from bottom: row 4 at bottom, row 1 at top.
        _HEIGHTS  = [0.56, 0.20, 0.12, 0.12]   # proportions, top→bottom in API
        _SPACING  = 0.04
        _AVAIL    = 1.0 - (len(_HEIGHTS) - 1) * _SPACING
        _H        = [r * _AVAIL for r in _HEIGHTS]   # [0.4928, 0.176, 0.1056, 0.1056]

        # Build bottom edges upward (Plotly row 1 = top)
        _bottoms = []
        cursor = 0.0
        for h in reversed(_H):          # row 4 first (bottom), then 3, 2, 1
            _bottoms.insert(0, cursor)
            cursor += h + _SPACING
        # _bottoms: [bottom of row1, row2, row3, row4]
        # row1 is top → largest y values; row4 is bottom → smallest y values
        _centers = [_bottoms[i] + _H[i] / 2 for i in range(4)]
        # _centers[0]=row1 Price, [1]=row2 Volume, [2]=row3 ADX, [3]=row4 ATR

        # ââ Create subplots ââââââââââââââââââââââââââââââââââââââââââââââ
        fig = make_subplots(
            rows=4, cols=1,
            shared_xaxes=True,
            vertical_spacing=_SPACING,
            row_heights=_HEIGHTS,
        )

        dates = df.index

        # ââ Row 1 : Price + SMA 50 + SMA 200 âââââââââââââââââââââââââââ

        fig.add_trace(go.Scatter(
            x=dates, y=df["_price"].round(4),
            name="Price",
            line=dict(color=COLOURS["price"], width=1.8),
            hovertemplate="<b>%{x|%Y-%m-%d}</b><br>Price: %{y:,.2f}<extra></extra>",
        ), row=1, col=1)

        if "sma_50" in df.columns:
            fig.add_trace(go.Scatter(
                x=dates, y=df["sma_50"].round(4),
                name="SMA 50",
                line=dict(color=COLOURS["sma_50"], width=1.4, dash="dot"),
                hovertemplate="SMA 50: %{y:,.2f}<extra></extra>",
            ), row=1, col=1)

        if "sma_200" in df.columns:
            fig.add_trace(go.Scatter(
                x=dates, y=df["sma_200"].round(4),
                name="SMA 200",
                line=dict(color=COLOURS["sma_200"], width=1.8, dash="dash"),
                hovertemplate="SMA 200: %{y:,.2f}<extra></extra>",
            ), row=1, col=1)

        if stop_price:
            fig.add_hline(
                y=stop_price,
                line_dash="dot",
                line_color=COLOURS["stop"],
                line_width=1.5,
                annotation_text=f"Stop: {stop_price:,.2f}",
                annotation_position="top left",
                annotation_font_color=COLOURS["stop"],
                row=1, col=1,
            )

        # ââ Row 2 : Volume ââââââââââââââââââââââââââââââââââââââââââââââ

        if "volume" in df.columns:
            fig.add_trace(go.Bar(
                x=dates, y=df["volume"],
                name="Volume",
                marker_color=COLOURS["volume"],
                opacity=0.7,
                hovertemplate="Vol: %{y:,.0f}<extra></extra>",
            ), row=2, col=1)

        # ââ Row 3 : ADX âââââââââââââââââââââââââââââââââââââââââââââââââ

        if "adx_14" in df.columns:
            fig.add_trace(go.Scatter(
                x=dates, y=df["adx_14"].round(2),
                name="ADX (14)",
                line=dict(color=COLOURS["adx"], width=1.6),
                fill="tozeroy",
                fillcolor=COLOURS["adx_fill"],
                hovertemplate="ADX: %{y:.1f}<extra></extra>",
            ), row=3, col=1)

            fig.add_hline(
                y=ADX_TREND_THRESHOLD,
                line_dash="dash",
                line_color="#DC2626",
                line_width=1.0,
                annotation_text=f"ADX threshold ({ADX_TREND_THRESHOLD})",
                annotation_position="top right",
                annotation_font_size=10,
                annotation_font_color="#DC2626",
                row=3, col=1,
            )

        # ââ Row 4 : ATR % âââââââââââââââââââââââââââââââââââââââââââââââ

        if "atr_20_pct" in df.columns:
            fig.add_trace(go.Scatter(
                x=dates, y=df["atr_20_pct"].round(3),
                name="ATR % (20)",
                line=dict(color=COLOURS["atr"], width=1.6),
                fill="tozeroy",
                fillcolor=COLOURS["atr_fill"],
                hovertemplate="ATR %%: %{y:.2f}%%<extra></extra>",
            ), row=4, col=1)

        # ââ Layout ââââââââââââââââââââââââââââââââââââââââââââââââââââââ
        #
        # t=110 accommodates a 2-line title (name + subtitle) above the chart.
        # The title block is part of the figure margin, so it can never
        # visually overlap with the plot area regardless of content length.
        #
        # l=80 is fixed and chosen to be large enough for all four y-axis
        # label annotations (see below). automargin is disabled on all y-axes
        # so Plotly does not dynamically widen the margin per panel.

        fig.update_layout(
            title=dict(
                text=title_text,
                font=dict(size=14, color="#0F172A"),
                x=0.0,
                xanchor="left",
                xref="paper",
                pad=dict(l=8, t=6),
            ),
            height=CHART_HEIGHT_PX,
            template="plotly_white",
            hovermode="x unified",
            showlegend=True,
            legend=dict(
                orientation="h",
                yanchor="bottom", y=1.01,
                xanchor="right",  x=1.0,
                font=dict(size=11),
                bgcolor="rgba(255,255,255,0.85)",
                bordercolor="#E2E8F0",
                borderwidth=1,
            ),
            margin=dict(l=80, r=30, t=110, b=40),
            paper_bgcolor=COLOURS["bg"],
            plot_bgcolor=COLOURS["bg"],
        )

        # ââ Y-axis formatting (NO title_text â labels are annotations below) ââ
        #
        # Omitting title_text from y-axes and using fixed paper-coord annotations
        # instead is the only reliable way to align all four labels at the same
        # horizontal x position, independent of tick-label widths.
        # automargin=False prevents Plotly from overriding the fixed l=80 margin.

        fig.update_yaxes(automargin=False, tickformat=",.2f",  row=1, col=1)
        fig.update_yaxes(automargin=False, tickformat=".2s",   row=2, col=1)
        fig.update_yaxes(automargin=False, range=[0, 80],      row=3, col=1)
        fig.update_yaxes(automargin=False, ticksuffix="%",     row=4, col=1)
        fig.update_xaxes(title_text="Date", row=4, col=1,
                         title_font=dict(size=11, color="#64748B"))

        # ââ Y-axis label annotations at fixed paper-coord x âââââââââââââ
        #
        # x=-0.035 places the label inside the l=80 margin at a consistent
        # horizontal position regardless of figure width.  All four labels
        # share exactly the same x, guaranteeing vertical alignment.
        #
        # y values are the vertical midpoints of each subplot row, computed
        # analytically from _HEIGHTS and _SPACING above.

        _LABEL_X     = -0.035
        _LABEL_STYLE = dict(size=11, color="#64748B")

        for row_idx, (label, y_ctr) in enumerate(
            zip(["Price", "Volume", "ADX", "ATR %"], _centers), start=1
        ):
            fig.add_annotation(
                x=_LABEL_X, y=y_ctr,
                xref="paper", yref="paper",
                text=label,
                showarrow=False,
                textangle=-90,
                font=_LABEL_STYLE,
                xanchor="center",
                yanchor="middle",
            )

        # Crosshair spikes
        fig.update_xaxes(
            showspikes=True, spikemode="across",
            spikethickness=1, spikecolor="#CBD5E1", spikedash="dot",
        )
        fig.update_yaxes(showspikes=False)

        return fig

    # ------------------------------------------------------------------
    # Dashboard assembly
    # ------------------------------------------------------------------

    def generate_dashboard(
        self,
        symbols:     List[str],
        output_path: Optional[str] = None,
    ) -> str:
        """
        Generate the single-file HTML dashboard.

        Process:
            1. Pre-load all data (reports warnings for missing symbols).
            2. Build Plotly figures, serialise each to a JSON config blob.
            3. Write a self-contained HTML file with embedded JS.

        Returns:
            Absolute path to the generated HTML file.
        """
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)

        if output_path is None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = str(REPORTS_DIR / f"technical_analysis_{ts}.html")

        logger.info(f"Pre-loading indicator data for {len(symbols)} symbols â¦")

        valid_entries: List[Dict] = []      # {symbol, name, sector, momentum, fig_json}

        for idx, symbol in enumerate(symbols, 1):
            logger.info(f"  [{idx:3d}/{len(symbols)}] Building chart: {symbol}")
            fig = self.build_figure(symbol)
            if fig is None:
                continue

            meta       = self._meta(symbol)
            mom_score  = self.momentum_map.get(symbol)
            pos_usd    = self.position_map.get(symbol)
            market_cap = meta.get("market_cap", None)

            valid_entries.append({
                "symbol":     symbol,
                "name":       meta.get("name", symbol),
                "sector":     meta.get("sector", "–"),
                "exchange":   meta.get("exchange", ""),
                "asset_type": meta.get("asset_type", "stock"),
                "market_cap": market_cap,
                "market_cap_fmt": _fmt_market_cap(market_cap),
                "momentum":   round(mom_score, 2) if mom_score is not None else None,
                "position":   round(pos_usd, 0)   if pos_usd is not None  else None,
                "qualified":  symbol in self.qualified_set,
                "_scenarios": meta.get("_scenarios", []),
                "fig_json":   fig.to_json(),
            })

        if not valid_entries:
            logger.error("No valid charts generated – check indicator files.")
            sys.exit(1)

        # ââ Sort entries by momentum descending (None sorts last) ââââââââ
        valid_entries.sort(
            key=lambda e: (e["momentum"] is None, -(e["momentum"] or 0))
        )

        # ââ Assign rank numbers after sort âââââââââââââââââââââââââââââââ
        for rank_idx, entry in enumerate(valid_entries, 1):
            entry["rank"] = rank_idx

        if not valid_entries:
            logger.error("No valid charts generated – check indicator files.")
            sys.exit(1)

        logger.info(f"Assembling HTML dashboard ({len(valid_entries)} charts) â¦")
        html = self._build_html(valid_entries)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)

        logger.info(f"â Dashboard saved: {output_path}")
        logger.info(f"  Symbols rendered : {len(valid_entries)}")
        logger.info(f"  Lookback         : {self.lookback_days} trading days")

        return output_path

    # ------------------------------------------------------------------
    # HTML template
    # ------------------------------------------------------------------

    def _build_html(self, entries: List[Dict]) -> str:
        """
        Assemble the final single-file HTML string.

        Sidebar features:
            - Symbols ordered #1 → #N by momentum rank
            - Text search (symbol / name / sector)
            - Exchange filter chips (All / NASDAQ / NYSE / AS / PA / XETRA)
            - Asset-type filter chips (All / Stock / ETF / Crypto)
            - Market cap displayed per item
        """
        # ââ Collect unique exchanges for filter chips ââââââââââââââââââââ

        known_exchanges = ["NASDAQ", "NYSE", "AS", "PA", "XETRA"]
        present_exchanges = sorted(
            {e["exchange"].upper() for e in entries if e.get("exchange")},
            key=lambda x: (x not in known_exchanges, x),
        )
        # Only show chips for exchanges that actually appear in the data
        exchange_chips = [ex for ex in known_exchanges if ex in present_exchanges]
        # Add any unknown exchanges not in the known list
        extra = [ex for ex in present_exchanges if ex not in known_exchanges]
        exchange_chips += extra

        # ââ Collect unique asset types ââââââââââââââââââââââââââââââââââââ
        present_types = {e.get("asset_type", "stock") for e in entries}
        type_chips = [t for t in ("stock", "etf", "crypto") if t in present_types]
        extra_types = [t for t in sorted(present_types) if t not in ("stock", "etf", "crypto")]
        type_chips += extra_types

        # ââ Collect unique scenarios ââââââââââââââââââââââââââââââââââââââ
        # Check if any entry has scenario tags (only present when --filter recommendations used)
        all_scenarios = set()
        has_portfolio_symbols = False
        for e in entries:
            scenarios = e.get("_scenarios", [])
            if scenarios:
                all_scenarios.update(scenarios)
                if "portfolio" in scenarios:
                    has_portfolio_symbols = True
        
        # Separate recommendation scenarios from portfolio
        recommendation_scenarios = {s for s in all_scenarios if s.startswith("scenario")}
        
        # Show filters based on mode, not just whether symbols have tags
        # This ensures filters are visible even when no recommendations file exists yet
        has_scenarios = self.show_recommendation_filters or len(recommendation_scenarios) > 0
        has_portfolio = self.show_recommendation_filters or has_portfolio_symbols

        # Known scenario order (for sorting)
        scenario_order = {"scenario1": 1, "scenario2": 2, "scenario3": 3}
        present_scenarios = sorted(recommendation_scenarios, key=lambda s: scenario_order.get(s, 99))

        # ââ Exchange filter chips HTML ââââââââââââââââââââââââââââââââââââ
        exch_chips_html = '<button class="chip active" data-filter="exchange" data-value="all" onclick="setFilter(\'exchange\',\'all\',this)">All</button>\n'
        for ex in exchange_chips:
            exch_chips_html += f'<button class="chip" data-filter="exchange" data-value="{ex.lower()}" onclick="setFilter(\'exchange\',\'{ex.lower()}\',this)">{ex}</button>\n'

        # ââ Asset type filter chips HTML ââââââââââââââââââââââââââââââââââ
        type_chips_html = '<button class="chip active" data-filter="type" data-value="all" onclick="setFilter(\'type\',\'all\',this)">All</button>\n'
        for t in type_chips:
            label = t.upper() if t == "etf" else t.capitalize()
            type_chips_html += f'<button class="chip" data-filter="type" data-value="{t}" onclick="setFilter(\'type\',\'{t}\',this)">{label}</button>\n'

        # ââ Portfolio filter chips HTML âââââââââââââââââââââââââââââââââââ
        portfolio_chips_html = ""
        portfolio_section_html = ""
        if has_portfolio:
            portfolio_chips_html = (
                '<button class="chip active" data-filter="portfolio" data-value="all" onclick="setFilter(\'portfolio\',\'all\',this)">All</button>\n'
                '<button class="chip" data-filter="portfolio" data-value="yes" onclick="setFilter(\'portfolio\',\'yes\',this)">In Portfolio</button>\n'
                '<button class="chip" data-filter="portfolio" data-value="no" onclick="setFilter(\'portfolio\',\'no\',this)">Not in Portfolio</button>\n'
            )
            portfolio_section_html = f'''
    <!-- Portfolio filter -->
    <div class="filter-section">
      <div class="filter-label">Portfolio</div>
      <div class="chips" id="portfolio-chips">
        {portfolio_chips_html}
      </div>
    </div>
'''

        # ââ Scenario filter chips HTML ââââââââââââââââââââââââââââââââââââ
        scenario_chips_html = ""
        scenario_section_html = ""
        if has_scenarios:
            scenario_chips_html = '<button class="chip active" data-filter="scenario" data-value="all" onclick="setFilter(\'scenario\',\'all\',this)">All</button>\n'
            scenario_labels = {
                "scenario1": "Scenario 1",
                "scenario2": "Scenario 2",
                "scenario3": "Scenario 3",
            }
            for s in present_scenarios:
                label = scenario_labels.get(s, s.capitalize())
                scenario_chips_html += f'<button class="chip" data-filter="scenario" data-value="{s}" onclick="setFilter(\'scenario\',\'{s}\',this)">{label}</button>\n'
            
            scenario_section_html = f'''
    <!-- Scenario filter -->
    <div class="filter-section">
      <div class="filter-label">Recommendation</div>
      <div class="chips" id="scenario-chips">
        {scenario_chips_html}
      </div>
    </div>
'''

        # ââ Sidebar nav items âââââââââââââââââââââââââââââââââââââââââââââ
        sidebar_items_html = ""
        for e in entries:
            rank     = e.get("rank", "–")
            mom_val  = e["momentum"]
            pos_cls  = "positive" if mom_val and mom_val > 0 else ("negative" if mom_val and mom_val < 0 else "neutral")
            mom_disp = f"{mom_val:+.1f}%" if mom_val is not None else "–"
            badge    = "â" if e["qualified"] else "â¬"
            safe_id  = e["symbol"].replace(".", "_")
            cap_str  = e.get("market_cap_fmt", "–")
            exch     = e.get("exchange", "").upper()
            atype    = e.get("asset_type", "stock")
            scenarios = e.get("_scenarios", [])
            scenarios_str = " ".join(scenarios) if scenarios else ""

            sidebar_items_html += f"""
            <div class="nav-item" id="nav-{safe_id}"
                 data-symbol="{e['symbol']}"
                 data-name="{e['name'].lower()}"
                 data-sector="{e['sector'].lower()}"
                 data-exchange="{exch.lower()}"
                 data-type="{atype}"
                 data-scenarios="{scenarios_str}"
                 onclick="showChart('{safe_id}')">
              <div class="nav-row1">
                <span class="nav-rank">#{rank}</span>
                <span class="nav-symbol">{badge} {e['symbol']}</span>
                <span class="mom-score {pos_cls}">{mom_disp}</span>
              </div>
              <div class="nav-name">{e['name'][:34]}</div>
              <div class="nav-row3">
                <span class="nav-tag exch-tag">{exch}</span>
                <span class="nav-tag type-tag">{atype.upper()}</span>
                <span class="nav-cap">{cap_str}</span>
              </div>
            </div>"""

        # ââ Chart divs âââââââââââââââââââââââââââââââââââââââââââââââââââ
        chart_divs_html = ""
        for i, e in enumerate(entries):
            safe_id = e["symbol"].replace(".", "_")
            display = "block" if i == 0 else "none"
            chart_divs_html += f"""
            <div id="chart-{safe_id}" class="chart-wrapper" style="display:{display};">
              <div id="plotly-{safe_id}" class="plotly-chart"></div>
            </div>"""

        # ââ Chart data blobs âââââââââââââââââââââââââââââââââââââââââââââ
        chart_data_js = "const CHART_DATA = {\n"
        for e in entries:
            safe_id = e["symbol"].replace(".", "_")
            chart_data_js += f'  "{safe_id}": {e["fig_json"]},\n'
        chart_data_js += "};\n"

        # ââ Summary stats âââââââââââââââââââââââââââââââââââââââââââââââââ
        n_total     = len(entries)
        n_qualified = sum(1 for e in entries if e["qualified"])
        ts_str      = datetime.now().strftime("%Y-%m-%d %H:%M")

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Technical Analysis Dashboard â {ts_str}</title>
  <script src="{PLOTLY_CDN}"></script>
  <style>
    /* ââ Reset âââââââââââââââââââââââââââââââââââââââââââââââââââââ */
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html, body {{
      height: 100%; overflow: hidden;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
                   Helvetica, Arial, sans-serif;
      font-size: 13px; background: {COLOURS["bg"]}; color: #1E293B;
    }}

    /* ââ Page layout âââââââââââââââââââââââââââââââââââââââââââââââ */
    #layout  {{ display: flex; height: 100vh; }}
    #sidebar {{ width: 295px; flex-shrink: 0; display: flex; flex-direction: column;
                background: {COLOURS["sidebar_bg"]}; color: #CBD5E1;
                overflow: hidden; border-right: 1px solid #334155; }}
    #main    {{ flex: 1; display: flex; flex-direction: column; overflow: hidden; }}

    /* ââ Sidebar header ââââââââââââââââââââââââââââââââââââââââââââ */
    #sidebar-header {{
      background: {COLOURS["header_bg"]}; padding: 12px 14px 10px;
      border-bottom: 2px solid {COLOURS["accent"]}; flex-shrink: 0;
    }}
    #sidebar-header h1 {{
      font-size: 14px; font-weight: 700; color: #F1F5F9;
      letter-spacing: 0.3px; margin-bottom: 3px;
    }}
    .stats {{ font-size: 11px; color: #94A3B8; }}
    .stats span {{ color: {COLOURS["accent"]}; font-weight: 600; }}

    /* ââ Search ââââââââââââââââââââââââââââââââââââââââââââââââââââ */
    #search-box {{
      padding: 8px 12px; flex-shrink: 0;
      border-bottom: 1px solid #334155; background: #1E293B;
    }}
    #search-input {{
      width: 100%; background: #0F172A; border: 1px solid #475569;
      border-radius: 6px; padding: 7px 10px; color: #E2E8F0;
      font-size: 12px; outline: none;
    }}
    #search-input::placeholder {{ color: #64748B; }}
    #search-input:focus {{ border-color: {COLOURS["accent"]}; }}

    /* ââ Filter chips ââââââââââââââââââââââââââââââââââââââââââââââ */
    .filter-section {{
      padding: 8px 10px 6px; flex-shrink: 0;
      border-bottom: 1px solid #334155; background: #1A2535;
    }}
    .filter-label {{
      font-size: 10px; font-weight: 600; color: #64748B;
      text-transform: uppercase; letter-spacing: 0.6px;
      margin-bottom: 5px;
    }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 4px; }}
    .chip {{
      padding: 3px 9px; border-radius: 12px; font-size: 11px;
      font-weight: 600; cursor: pointer; border: 1px solid #334155;
      background: #273549; color: #94A3B8;
      transition: all 0.15s; white-space: nowrap;
    }}
    .chip:hover {{ border-color: {COLOURS["accent"]}; color: #E2E8F0; }}
    .chip.active {{
      background: {COLOURS["accent"]}; color: #0F172A;
      border-color: {COLOURS["accent"]};
    }}

    /* ââ Nav list ââââââââââââââââââââââââââââââââââââââââââââââââââ */
    #nav-list {{ flex: 1; overflow-y: auto; padding: 4px 0; }}
    #nav-list::-webkit-scrollbar {{ width: 4px; }}
    #nav-list::-webkit-scrollbar-thumb {{ background: #475569; border-radius: 3px; }}

    .nav-item {{
      padding: 9px 12px 8px; cursor: pointer;
      border-left: 3px solid transparent;
      transition: background 0.12s;
    }}
    .nav-item:hover  {{ background: {COLOURS["sidebar_hover"]}; }}
    .nav-item.active {{
      background: {COLOURS["sidebar_hover"]};
      border-left-color: {COLOURS["accent"]};
    }}
    .nav-item.hidden {{ display: none; }}

    /* Nav item rows */
    .nav-row1 {{ display: flex; align-items: center; gap: 5px; margin-bottom: 2px; }}
    .nav-rank   {{ font-size: 10px; color: #475569; font-weight: 700;
                   min-width: 24px; }}
    .nav-symbol {{ font-size: 12px; font-weight: 700; color: #E2E8F0; flex: 1; }}
    .nav-name   {{ font-size: 11px; color: #94A3B8; margin-bottom: 4px;
                   white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .nav-row3   {{ display: flex; align-items: center; gap: 4px; }}
    .nav-tag    {{ font-size: 9px; font-weight: 700; padding: 1px 5px;
                   border-radius: 4px; text-transform: uppercase; }}
    .exch-tag   {{ background: #1D3557; color: #90CAF9; }}
    .type-tag   {{ background: #1B4332; color: #6EE7B7; }}
    .nav-cap    {{ font-size: 10px; color: #64748B; margin-left: auto; }}

    /* Momentum score badge */
    .mom-score          {{ font-size: 11px; font-weight: 700; }}
    .mom-score.positive {{ color: {COLOURS["positive"]}; }}
    .mom-score.negative {{ color: {COLOURS["negative"]}; }}
    .mom-score.neutral  {{ color: {COLOURS["neutral"]};  }}

    /* ââ Main top bar âââââââââââââââââââââââââââââââââââââââââââââââ */
    #top-bar {{
      flex-shrink: 0; background: white; border-bottom: 1px solid #E2E8F0;
      padding: 10px 20px; display: flex; align-items: center;
      justify-content: space-between;
    }}
    .symbol-label {{ font-size: 15px; font-weight: 700; color: #1E293B; }}
    .meta-label   {{ font-size: 11px; color: #94A3B8; }}

    /* ââ Chart area ââââââââââââââââââââââââââââââââââââââââââââââââ */
    #chart-area {{
      flex: 1; overflow-y: auto; background: {COLOURS["bg"]};
    }}
    #chart-area::-webkit-scrollbar {{ width: 6px; }}
    #chart-area::-webkit-scrollbar-thumb {{ background: #CBD5E1; border-radius: 3px; }}
    .chart-wrapper  {{ padding: 8px 12px 16px; }}
    .plotly-chart   {{ width: 100%; min-height: {CHART_HEIGHT_PX}px; }}

    /* ââ Empty state âââââââââââââââââââââââââââââââââââââââââââââââ */
    #no-results {{
      display: none; padding: 20px; color: #64748B;
      font-size: 12px; text-align: center;
    }}
  </style>
</head>
<body>
<div id="layout">

  <!-- âââ SIDEBAR âââââââââââââââââââââââââââââââââââââââââââââââââââââââ -->
  <div id="sidebar">

    <div id="sidebar-header">
      <h1>ð Technical Analysis</h1>
      <div class="stats">
        {ts_str} &nbsp;|&nbsp;
        <span>{n_total}</span> charts &nbsp;|&nbsp;
        <span>{n_qualified}</span> qualified
      </div>
    </div>

    <div id="search-box">
      <input id="search-input" type="text"
             placeholder="Search symbol, name or sector â¦"
             oninput="applyFilters()" autocomplete="off" />
    </div>

    <!-- Exchange filter -->
    <div class="filter-section">
      <div class="filter-label">Exchange</div>
      <div class="chips" id="exchange-chips">
        {exch_chips_html}
      </div>
    </div>

    <!-- Asset type filter -->
    <div class="filter-section">
      <div class="filter-label">Type</div>
      <div class="chips" id="type-chips">
        {type_chips_html}
      </div>
    </div>

{portfolio_section_html}
{scenario_section_html}

    <div id="nav-list">
      {sidebar_items_html}
      <div id="no-results">No symbols match the current filters.</div>
    </div>

  </div>
  <!-- âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ -->

  <!-- âââ MAIN ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ -->
  <div id="main">
    <div id="top-bar">
      <span class="symbol-label" id="active-label">← Select a symbol</span>
      <span class="meta-label">
        Lookback: {self.lookback_days} trading days &nbsp;|&nbsp; Architecture v3.2
      </span>
    </div>
    <div id="chart-area">
      {chart_divs_html}
    </div>
  </div>
  <!-- âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ -->

</div>

<script>
// ââ Chart data ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
{chart_data_js}

// ââ Active filter state âââââââââââââââââââââââââââââââââââââââââââââââââââ
const activeFilters = {{ exchange: "all", type: "all", portfolio: "all", scenario: "all" }};
const rendered = {{}};
let activeId = null;

// ââ Set a filter and refresh ââââââââââââââââââââââââââââââââââââââââââââââ
function setFilter(dimension, value, btn) {{
  activeFilters[dimension] = value;

  // Update chip active state for that dimension
  const groupMap = {{
    exchange: "exchange-chips",
    type: "type-chips",
    portfolio: "portfolio-chips",
    scenario: "scenario-chips"
  }};
  const groupId = groupMap[dimension];
  if (groupId) {{
    document.querySelectorAll(`#${{groupId}} .chip`).forEach(c => c.classList.remove("active"));
    btn.classList.add("active");
  }}

  applyFilters();
}}

// ââ Apply all active filters + text search âââââââââââââââââââââââââââââââ
function applyFilters() {{
  const query           = document.getElementById("search-input").value.trim().toLowerCase();
  const exchFilter      = activeFilters.exchange;
  const typeFilter      = activeFilters.type;
  const portfolioFilter = activeFilters.portfolio;
  const scenarioFilter  = activeFilters.scenario;
  const items           = document.querySelectorAll(".nav-item");
  let shown = 0;

  items.forEach(function(el) {{
    const sym       = el.getAttribute("data-symbol").toLowerCase();
    const name      = el.getAttribute("data-name").toLowerCase();
    const sector    = el.getAttribute("data-sector").toLowerCase();
    const exch      = el.getAttribute("data-exchange").toLowerCase();
    const type      = el.getAttribute("data-type").toLowerCase();
    const scenarios = el.getAttribute("data-scenarios") || "";  // space-separated list
    
    const scenarioList = scenarios.split(" ").filter(s => s.length > 0);
    const inPortfolio  = scenarioList.includes("portfolio");

    const textMatch      = !query || sym.includes(query) || name.includes(query) || sector.includes(query);
    const exchMatch      = exchFilter === "all" || exch === exchFilter;
    const typeMatch      = typeFilter === "all" || type === typeFilter;
    const portfolioMatch = portfolioFilter === "all" || 
                          (portfolioFilter === "yes" && inPortfolio) ||
                          (portfolioFilter === "no" && !inPortfolio);
    // Scenario match: check if active scenario appears in the list (excluding "portfolio")
    const scenarioMatch  = scenarioFilter === "all" || scenarioList.includes(scenarioFilter);

    if (textMatch && exchMatch && typeMatch && portfolioMatch && scenarioMatch) {{
      el.classList.remove("hidden");
      shown++;
    }} else {{
      el.classList.add("hidden");
    }}
  }});

  document.getElementById("no-results").style.display = shown === 0 ? "block" : "none";

  // If the currently displayed chart was just hidden by this filter,
  // auto-select the first still-visible item so the main panel always
  // shows a symbol that matches the active filter state.
  if (activeId) {{
    const activeNav = document.getElementById("nav-" + activeId);
    if (activeNav && activeNav.classList.contains("hidden")) {{
      const first = document.querySelector(".nav-item:not(.hidden)");
      if (first) {{
        const safeId = first.getAttribute("data-symbol").replace(/\\./g, "_");
        showChart(safeId);
      }}
    }}
  }}
}}

// ââ Show a chart (lazy Plotly render) ââââââââââââââââââââââââââââââââââââ
function showChart(safeId) {{
  if (activeId && activeId !== safeId) {{
    const prev = document.getElementById("chart-" + activeId);
    if (prev) prev.style.display = "none";
    const prevNav = document.getElementById("nav-" + activeId);
    if (prevNav) prevNav.classList.remove("active");
  }}

  const wrapper = document.getElementById("chart-" + safeId);
  if (wrapper) wrapper.style.display = "block";

  const navEl = document.getElementById("nav-" + safeId);
  if (navEl) {{
    navEl.classList.add("active");
    const sym  = navEl.querySelector(".nav-symbol").textContent.trim();
    const name = navEl.querySelector(".nav-name").textContent.trim();
    document.getElementById("active-label").textContent = sym + "  â  " + name;
  }}

  activeId = safeId;

  if (!rendered[safeId]) {{
    const plotEl = document.getElementById("plotly-" + safeId);
    const data   = CHART_DATA[safeId];
    if (plotEl && data) {{
      Plotly.react(plotEl, data.data, data.layout, {{
        responsive: true, displaylogo: false,
        modeBarButtonsToRemove: ["lasso2d","select2d"],
      }});
      rendered[safeId] = true;
    }}
  }}
}}

// ââ Keyboard shortcuts ââââââââââââââââââââââââââââââââââââââââââââââââââââ
document.addEventListener("keydown", function(e) {{
  if (e.key === "/" && document.activeElement.tagName !== "INPUT") {{
    e.preventDefault();
    document.getElementById("search-input").focus();
  }}
  if (e.key === "Escape") {{
    document.getElementById("search-input").value = "";
    applyFilters();
  }}
}});

// ââ Auto-select first visible symbol on load âââââââââââââââââââââââââââââ
window.addEventListener("load", function() {{
  const first = document.querySelector(".nav-item:not(.hidden)");
  if (first) {{
    const safeId = first.getAttribute("data-symbol").replace(/\\./g, "_");
    showChart(safeId);
  }}
}});

// ââ Resize handler ââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
let resizeTimer;
window.addEventListener("resize", function() {{
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(function() {{
    if (activeId && rendered[activeId]) {{
      const plotEl = document.getElementById("plotly-" + activeId);
      if (plotEl) Plotly.relayout(plotEl, {{autosize: true}});
    }}
  }}, 250);
}});
</script>
</body>
</html>"""


# ============================================================================
# CLI ARGUMENT PARSING
# ============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate interactive technical analysis HTML dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Top 20 by momentum (default)
  python scripts/15_generate_technical_charts.py

  # Explicit symbol list
  python scripts/15_generate_technical_charts.py --symbols AAPL.US,MSFT.US,NVDA.US

  # All qualified-trend symbols
  python scripts/15_generate_technical_charts.py --filter qualified

  # All screened symbols
  python scripts/15_generate_technical_charts.py --filter all

  # Custom lookback and top-N
  python scripts/15_generate_technical_charts.py --lookback 300 --top 30

  # Save to specific path
  python scripts/15_generate_technical_charts.py --output /tmp/review.html
        """,
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default=None,
        help="Comma-separated symbol list (overrides --filter). E.g. AAPL.US,MSFT.US",
    )
    parser.add_argument(
        "--filter",
        choices=["top", "recommendations", "qualified", "all"],
        default="recommendations",
        help=(
            "Symbol selection mode when --symbols is not set. "
            "'recommendations' = all qualified symbols with scenario tags (default), "
            "'top' = top N by momentum, "
            "'qualified' = all qualified-trend symbols, "
            "'all' = all screened symbols."
        ),
    )
    parser.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP_N,
        help=f"How many symbols to include when --filter top (default: {DEFAULT_TOP_N})",
    )
    parser.add_argument(
        "--lookback",
        type=int,
        default=DEFAULT_LOOKBACK,
        help=f"Trading-day lookback window per chart (default: {DEFAULT_LOOKBACK})",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output HTML file path (default: reports/charts/technical_analysis_<ts>.html)",
    )
    return parser.parse_args()


# ============================================================================
# MAIN
# ============================================================================

def main() -> None:
    args = parse_args()

    logger.info("=" * 70)
    logger.info("TECHNICAL ANALYSIS CHART GENERATOR â Script 15")
    logger.info("=" * 70)
    logger.info(f"Execution time : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"Lookback       : {args.lookback} trading days")
    logger.info(f"Filter mode    : {args.filter}")
    logger.info(f"Top N          : {args.top}")
    logger.info(f"Explicit syms  : {args.symbols or 'â'}")
    logger.info("")

    # ââ Validate indicators directory âââââââââââââââââââââââââââââââââ
    if not INDICATORS_DIR.exists():
        logger.error(f"Indicators directory not found: {INDICATORS_DIR}")
        logger.error("Run Script 5 (05_calculate_indicators.py) first.")
        sys.exit(1)

    n_ind = len(list(INDICATORS_DIR.glob("*_indicators.parquet")))
    if n_ind == 0:
        logger.error(f"No indicator files found in {INDICATORS_DIR}")
        logger.error("Run Script 5 (05_calculate_indicators.py) first.")
        sys.exit(1)

    logger.info(f"Found {n_ind} indicator file(s) in cache")

    # ââ Load enrichment data âââââââââââââââââââââââââââââââââââââââââââ
    logger.info("Loading metadata and signal files â¦")

    metadata       = load_metadata()
    ranked         = load_momentum_ranked()
    qualified_list = load_qualified_trends()
    position_sizes = load_position_sizes()
    stop_levels    = load_stop_levels()

    # ââ Merge market cap from company_info.json âââââââââââââââââââââââ
    #
    # THE KEY MISMATCH PROBLEM (and its fix):
    #
    # company_info.json is keyed by bare tickers (e.g. "AAPL") because
    # Script 2 reads them from raw_bulk parquet "code" columns which
    # carry no exchange suffix.
    #
    # metadata is keyed by full EODHD symbols (e.g. "AAPL.US") from
    # qualified_symbols.json (Script 4).
    #
    # Direct dict lookup (sym in metadata) therefore never matches.
    # We bridge the two via build_base_ticker_index():
    #   "AAPL" → "AAPL.US", "MSFT" → "MSFT.US", etc.

    _CAP_KEYS = ("market_cap", "marketCap", "MarketCap", "mktCap")

    company_info_raw = load_company_info()
    base_index       = build_base_ticker_index(metadata)   # bare → full EODHD sym

    logger.info(f"  company_info.json : {len(company_info_raw)} entries")
    logger.info(f"  Base ticker index : {len(base_index)} entries")

    n_cap_found  = 0
    n_cap_merged = 0
    n_key_miss   = 0

    # REPLACE WITH:
    for ci_key, info in company_info_raw.items():
        if not isinstance(info, dict):
            continue

        # company_info.json is keyed by full EODHD symbols (e.g. "EWY.NYSE"),
        # identical to the metadata dict keys — direct lookup, no index needed.
        # The base_index indirection was written under a wrong assumption that
        # company_info used bare tickers; the actual data uses full symbols.
        full_sym = ci_key if ci_key in metadata else None
        if full_sym is None:
            n_key_miss += 1
            continue

        # ── Merge instrument_type unconditionally ──────────────────────────
        # Must NOT be gated on market_cap: ETFs often have no market_cap in
        # company_info.json, so they would otherwise keep the default "stock".
        inst_type = info.get("instrument_type")
        if inst_type:
            metadata[full_sym]["asset_type"] = inst_type

        # ── Merge market_cap only when present and positive ────────────────
        cap_val = None
        for _k in _CAP_KEYS:
            _v = info.get(_k)
            if _v is not None:
                cap_val = _v
                break

        if cap_val is not None:
            try:
                cap_float = float(cap_val)
                if cap_float > 0:
                    metadata[full_sym]["market_cap"] = cap_float
                    n_cap_found += 1
                    n_cap_merged += 1
            except (TypeError, ValueError):
                pass

    logger.info(
        f"  Market cap        : {n_cap_found} entries with cap>0  |  "
        f"{n_cap_merged} merged into metadata  |  "
        f"{n_key_miss} unmatched (not in qualified universe)"
    )
    if n_cap_found > 0 and n_cap_merged == 0:
        sample_key = next(iter(company_info_raw))
        sample_base_keys = list(base_index.keys())[:5]
        logger.warning(
            f"  â Merge produced 0 results despite {n_cap_found} cap entries. "
            f"Sample company_info key: '{sample_key}'. "
            f"Sample base_index keys: {sample_base_keys}. "
            f"Check that key formats match."
        )

    # Build momentum lookup: symbol → score
    # Primary source: momentum_ranked.json  (Script 7 output)
    # Fallback:       qualified_trends.json (Script 6 – has close + sma_200)
    momentum_map: Dict[str, float] = {}
    for r in ranked:
        if isinstance(r, dict) and "symbol" in r and "momentum_score" in r:
            try:
                momentum_map[r["symbol"]] = float(r["momentum_score"])
            except (TypeError, ValueError):
                pass

    if not momentum_map:
        raw_qt = _load_json(SIGNALS_DIR / "qualified_trends.json")
        if isinstance(raw_qt, dict) and "symbols" in raw_qt:
            for sym, snap in raw_qt["symbols"].items():
                if not isinstance(snap, dict):
                    continue
                try:
                    close   = float(snap.get("close",   0) or 0)
                    sma_200 = float(snap.get("sma_200", 0) or 0)
                    if sma_200 > 0:
                        momentum_map[sym] = round((close - sma_200) / sma_200 * 100, 4)
                except (TypeError, ValueError):
                    pass
        if momentum_map:
            logger.info(
                f"  Momentum scores derived from qualified_trends.json "
                f"({len(momentum_map)} symbols)"
            )

    qualified_set = set(qualified_list)

    logger.info(f"  Metadata entries  : {len(metadata)}")
    logger.info(f"  Momentum scores   : {len(momentum_map)}")
    logger.info(f"  Qualified trends  : {len(qualified_set)}")
    logger.info(f"  Active positions  : {len(position_sizes)}")
    logger.info(f"  Stop levels       : {len(stop_levels)}")

    # ââ Load recommendation tags for filtering (all modes) âââââââââââââ
    # Load recommendation/portfolio tags regardless of filter mode.
    # This allows Portfolio and Recommendation filters to work with any symbol set.
    symbol_sources, portfolio_symbols = load_rebalancing_recommendations()
    
    # Build scenario tags for all symbols
    global _SCENARIO_TAGS
    _SCENARIO_TAGS = {}
    
    # Tag symbols from metadata with scenario/portfolio membership
    n_portfolio_matched = 0
    for sym in metadata.keys():
        tags = symbol_sources.get(sym, []).copy()
        if sym in portfolio_symbols:
            tags.append("portfolio")
            n_portfolio_matched += 1
        if tags:  # Only store if symbol has at least one tag
            _SCENARIO_TAGS[sym] = tags
    
    if symbol_sources or portfolio_symbols:
        n_scenario1 = sum(1 for s in symbol_sources.values() if 'scenario1' in s)
        n_scenario2 = sum(1 for s in symbol_sources.values() if 'scenario2' in s)
        n_scenario3 = sum(1 for s in symbol_sources.values() if 'scenario3' in s)
        n_tagged = len(_SCENARIO_TAGS)
        
        logger.info(f"  Recommendation tags loaded:")
        logger.info(f"    - Scenario 1     : {n_scenario1} symbols")
        logger.info(f"    - Scenario 2     : {n_scenario2} symbols")
        logger.info(f"    - Scenario 3     : {n_scenario3} symbols")
        logger.info(f"    - Portfolio (raw): {len(portfolio_symbols)} symbols")
        logger.info(f"    - Portfolio (matched): {n_portfolio_matched} symbols in metadata")
        logger.info(f"    - Tagged (any)   : {n_tagged} symbols")
        
        # Debug: show portfolio symbols that didn't match
        if portfolio_symbols and n_portfolio_matched == 0:
            sample_portfolio = list(portfolio_symbols)[:5]
            sample_metadata = list(metadata.keys())[:5]
            logger.warning(
                f"  â  Portfolio symbols found but none matched metadata!"
            )
            logger.warning(f"    Sample portfolio keys: {sample_portfolio}")
            logger.warning(f"    Sample metadata keys: {sample_metadata}")
        elif portfolio_symbols and n_portfolio_matched < len(portfolio_symbols):
            unmatched = portfolio_symbols - set(metadata.keys())
            logger.warning(
                f"  â  {len(unmatched)} portfolio symbols not in metadata (not qualified)"
            )

    # ââ Resolve symbols ââââââââââââââââââââââââââââââââââââââââââââââââ
    symbols = resolve_symbols(
        cli_symbols=args.symbols,
        filter_mode=args.filter,
        top_n=args.top,
    )

    if not symbols:
        logger.error("Symbol list is empty – nothing to chart.")
        sys.exit(1)

    # ââ Merge scenario tags into metadata ââââââââââââââââââââââââââââââââ
    # If --filter recommendations was used, resolve_symbols() populated the
    # global _SCENARIO_TAGS dict. Merge those tags into metadata so the
    # sidebar can show which scenarios recommend each symbol.
    if _SCENARIO_TAGS:
        for sym, tags in _SCENARIO_TAGS.items():
            if sym in metadata:
                metadata[sym]["_scenarios"] = tags
            else:
                # Symbol in recommendations but not in qualified_symbols.json
                # (shouldn't happen in production, but handle it gracefully)
                metadata[sym] = {
                    "name": sym,
                    "sector": "Unknown",
                    "exchange": "",
                    "asset_type": "stock",
                    "market_cap": None,
                    "_base_ticker": sym.rsplit(".", 1)[0],
                    "_scenarios": tags,
                }
        logger.info(f"  Scenario tags merged for {len(_SCENARIO_TAGS)} symbols")

    logger.info(f"\nCharting {len(symbols)} symbol(s)")

    # ââ Build and save dashboard âââââââââââââââââââââââââââââââââââââââ
    # Show recommendation filters if recommendations/portfolio data exists
    # This works with all filter modes (recommendations, all, qualified, top)
    show_filters = len(_SCENARIO_TAGS) > 0
    
    generator = TechnicalChartGenerator(
        lookback_days=args.lookback,
        metadata=metadata,
        momentum_map=momentum_map,
        position_map=position_sizes,
        stop_map=stop_levels,
        qualified_set=qualified_set,
        show_recommendation_filters=show_filters,
    )

    output_path = generator.generate_dashboard(
        symbols=symbols,
        output_path=args.output,
    )

    logger.info("")
    logger.info("â Done. Open in your browser:")
    logger.info(f"   file://{output_path}")
    logger.info("")


if __name__ == "__main__":
    main()
