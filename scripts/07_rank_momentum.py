#!/usr/bin/env python3
"""
Script 7: Momentum Ranker
==========================
Rank all trend-qualified instruments by momentum score for portfolio selection

Purpose:
    After trend qualification (Script 6), this script applies a momentum scoring
    formula to every qualified instrument and produces a ranked list.
    The downstream rebalancer (Script 11) uses the top-N entries from this list
    directly — no further filtering or discretion is applied.

Momentum Score Formulas:
    sma_dist (default / production):
        Momentum_Score = ((Close - SMA_slow) / SMA_slow) * 100

        Interpretation:
            Positive = price is above SMA_slow (confirmed uptrend)
            Higher   = stronger distance above long-term mean (stronger trend)
            Example  : Score of 18.5 -> price is 18.5% above its SMA_slow

        Formula rationale:
            - Simple and transparent — auditable in one line
            - Comparable across all instruments and asset classes
            - Penalises instruments that barely cleared qualification threshold
            - Rewards instruments with deep, sustained trends
            - Academic support: Moskowitz, Ooi & Pedersen (2012) — time-series momentum

    roc_weight (experiment / paper trading):
        Momentum_Score = sum(w_i * ROC_period_i) * 100
        Default weights: [0.20, 0.30, 0.50] over [20d, 60d, 120d]

        Formula rationale:
            - Rewards recent acceleration (Jegadeesh & Titman 1993)
            - Long-biased weight preset confirmed by full_v3 WFO (11/11 windows)

    Supplementary ROC metrics (stored, NOT used for ranking):
        ROC_20d  = (Close[t] / Close[t-20])  - 1
        ROC_60d  = (Close[t] / Close[t-60])  - 1
        ROC_120d = (Close[t] / Close[t-120]) - 1
        These are written to the output for transparency and downstream reporting.

Formula name convention:
    Short names are canonical throughout the codebase:
        'sma_dist'   — matches scripts 16, 17, strategies.json, strategy_parameters.json
        'roc_weight' — matches scripts 16, 17, strategies.json, strategy_parameters_exp_roc.json
    Long-form aliases ('sma_distance', 'roc_weighted') are accepted for backwards
    compatibility but normalised to the short form before dispatch.

Dependencies:
    - Script 4: Universe Screener  (qualified_symbols.json — for metadata)
    - Script 5: Indicator Calculator (indicators/*.parquet)
    - Script 6: Trend Qualifier     (qualified_trends.json — must exist)

Inputs:
    - data_cache/signals/qualified_trends.json
    - data_cache/indicators/{symbol}_indicators.parquet   (one file per symbol)
    - data_cache/qualified/qualified_symbols.json          (for name/sector/exchange)
    - data_cache/fundamentals/company_info.json

Outputs:
    - data_cache/signals/{strategy}/momentum_ranked.json    (ranked list, all qualified)
    - data_cache/signals/{strategy}/momentum_summary.json   (run statistics)
    - reports/signals/{strategy}/{YYYYMMDD}_momentum_ranked.csv  (human-readable)

Execution:
    python scripts/07_rank_momentum.py --as-of-date 2026-01-31
    python scripts/07_rank_momentum.py --as-of-date 2026-01-31 --strategy sma_dist
    python scripts/07_rank_momentum.py --as-of-date 2026-01-31 --strategy all

Architecture: v3.9 (Mar 2026) — multi-strategy via --strategy flag
"""

import os
import sys
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
import numpy as np

# ============================================================================
# CONFIGURATION
# ============================================================================

PROJECT_ROOT      = Path(__file__).parent.parent
DATA_CACHE_DIR    = PROJECT_ROOT / "data_cache"
DATA_LOAD_DIR     = PROJECT_ROOT.parent / "data_load" / "data_cache"
# Input
INDICATORS_DIR    = DATA_CACHE_DIR / "indicators"
QUALIFIED_DIR     = DATA_CACHE_DIR / "qualified"
# Output
SIGNALS_DIR       = DATA_CACHE_DIR / "signals"
fundamentals_file = DATA_LOAD_DIR / 'fundamentals' / 'company_info.json'
REPORTS_DIR       = PROJECT_ROOT / "reports" / "signals"
LOG_DIR           = PROJECT_ROOT / "logs"

# ---------------------------------------------------------------------------
# Load centralized parameters.
# ---------------------------------------------------------------------------
import sys as _sys
_sys.path.insert(0, str(PROJECT_ROOT))
from config.params import P, ConfigurationError
from config.strategies import resolve_strategies, add_strategy_argument, StrategyDef

# Momentum formula parameters — sourced from config/strategy_parameters.json.
MOMENTUM_SMA_PERIOD = P.momentum.sma_period

# Supplementary ROC lookback periods (stored, NOT used for ranking).
ROC_PERIODS = {f"roc_{n}d": n for n in P.momentum.roc_periods}

# Minimum bars required to compute the longest ROC.
MIN_BARS_REQUIRED = MOMENTUM_SMA_PERIOD + max(P.momentum.roc_periods) + 10

# ============================================================================
# LOGGING
# ============================================================================

def setup_logging() -> logging.Logger:
    """Configure file + console logging"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = LOG_DIR / f'rank_momentum_{timestamp}.log'

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )
    logger = logging.getLogger(__name__)
    logger.info(f"Log file: {log_file}")
    return logger

logger = setup_logging()

# ============================================================================
# DATA LOADING
# ============================================================================

def load_qualified_trends(as_of_date: str) -> Dict:
    """
    Load trend-qualified instruments from Script 6 output.

    Args:
        as_of_date: Reference date (YYYY-MM-DD)

    Returns:
        Dict keyed by symbol, each value contains:
            symbol, sma_fast, sma_slow, close, adx, atr_pct, qualified_date

    Raises:
        SystemExit if file not found or empty
    """
    signals_file = SIGNALS_DIR / 'qualified_trends.json'

    if not signals_file.exists():
        logger.error(f"qualified_trends.json not found at {signals_file}")
        logger.error("Please run Script 6 (06_qualify_trends.py) first")
        sys.exit(1)

    with open(signals_file, 'r') as f:
        data = json.load(f)

    if not data:
        logger.error("qualified_trends.json is empty — no symbols qualified")
        sys.exit(1)

    logger.info(f"Loaded {len(data)} qualified trends from {signals_file}")
    return data["symbols"]


def load_qualified_metadata() -> Dict:
    """
    Load symbol metadata (name, exchange, sector, asset_class) from Script 4 output.
    Also loads instrument_type from fundamentals (company_info.json).

    Returns:
        Dict keyed by symbol, or empty dict if file not available
    """
    metadata_file = QUALIFIED_DIR / 'qualified_symbols.json'

    if not metadata_file.exists():
        logger.warning(f"qualified_symbols.json not found at {metadata_file}")
        logger.warning("Proceeding without metadata (name/sector/exchange will be empty)")
        return {}

    with open(metadata_file, 'r') as f:
        raw = json.load(f)

    # qualified_symbols.json is a list of dicts — index by symbol
    metadata = {}
    for item in raw:
        sym = item.get('symbol')
        if sym:
            metadata[sym] = item

    logger.info(f"Loaded metadata for {len(metadata)} symbols")

    # Load fundamentals to get instrument_type (asset_class)
    if fundamentals_file.exists():
        try:
            with open(fundamentals_file, 'r') as f:
                fundamentals = json.load(f)

            matched = 0
            for sym, meta in metadata.items():
                if sym in fundamentals:
                    meta['instrument_type'] = fundamentals[sym].get('instrument_type', '')
                    matched += 1
                else:
                    meta['instrument_type'] = ''

            logger.info(f"Matched instrument_type for {matched}/{len(metadata)} symbols")
        except Exception as e:
            logger.warning(f"Failed to load fundamentals: {e}")
            for meta in metadata.values():
                meta['instrument_type'] = ''
    else:
        logger.warning(f"company_info.json not found at {fundamentals_file}")
        for meta in metadata.values():
            meta['instrument_type'] = ''

    return metadata


def load_indicator_data(symbol: str) -> Optional[pd.DataFrame]:
    """
    Load pre-calculated indicator data for a symbol from Script 5 output.

    Expected columns:
        close, high, low, open, volume,
        adjusted_close, sma_fast, sma_slow, atr_pct, adx

    Args:
        symbol: Instrument identifier (e.g. 'AAPL.US')

    Returns:
        DataFrame indexed by date (ascending), or None if not found
    """
    indicator_file = INDICATORS_DIR / f"{symbol}_indicators.parquet"

    if not indicator_file.exists():
        logger.debug(f"No indicator file for {symbol}")
        return None

    try:
        df = pd.read_parquet(indicator_file)

        # Ensure DatetimeIndex
        if df.index.name == 'date' or df.index.dtype == 'object':
            df.index = pd.to_datetime(df.index)
        elif 'date' in df.columns:
            df = df.set_index('date')
            df.index = pd.to_datetime(df.index)

        df.sort_index(inplace=True)
        return df

    except Exception as e:
        logger.warning(f"Failed to load indicators for {symbol}: {e}")
        return None


# ============================================================================
# MOMENTUM CALCULATION
# ============================================================================

def _load_experiment_momentum_config(config_path: str) -> dict:
    """
    Load the momentum section from a strategy parameters JSON file.
    Returns a dict with keys: formula, sma_period, roc_periods, roc_weights.
    Falls back to production P values for any key not present.
    """
    import json as _json
    with open(config_path, "r", encoding="utf-8") as fh:
        raw = _json.load(fh)
    mom = raw.get("momentum", {})
    return {
        "formula":     mom.get("formula",     "sma_dist"),
        "sma_period":  mom.get("sma_period",  P.momentum.sma_period),
        "roc_periods": mom.get("roc_periods", list(P.momentum.roc_periods)),
        "roc_weights": mom.get("roc_weights", [0.20, 0.30, 0.50]),
    }


def calculate_momentum_score(
    df: pd.DataFrame,
    as_of_date: str,
    symbol: str,
    formula: str = "sma_dist",
    active_roc_periods: Optional[Dict] = None,
    roc_weights: Optional[List[float]] = None,
) -> Tuple[Optional[float], Dict]:
    """
    Calculate momentum score using one of two formulas.

    'sma_dist'   (default): Score = ((Close - SMA_slow) / SMA_slow) * 100
    'roc_weight' (experiment): Score = sum(w_i * ROC_i) * 100

    Args:
        df:                 Indicator DataFrame indexed by date.
        as_of_date:         Reference date string (YYYY-MM-DD).
        symbol:             Instrument identifier (used in log messages only).
        formula:            'sma_dist' or 'roc_weight'. Long-form aliases
                            ('sma_distance', 'roc_weighted') are accepted.
        active_roc_periods: Dict mapping label -> n_bars, e.g. {'roc_20d': 20}.
                            Defaults to module-level ROC_PERIODS.
        roc_weights:        Weight list aligned to active_roc_periods values.
                            Used only when formula='roc_weight'.

    Returns:
        Tuple of (momentum_score, supplementary_roc_dict).
        Both are None / {} if data is insufficient or formula is unknown.
    """
    if active_roc_periods is None:
        active_roc_periods = ROC_PERIODS
    if roc_weights is None:
        roc_weights = [0.20, 0.30, 0.50]

    as_of_dt   = pd.Timestamp(as_of_date)
    df_to_date = df[df.index <= as_of_dt]

    if df_to_date.empty:
        return None, {}

    min_bars = MOMENTUM_SMA_PERIOD + max(active_roc_periods.values()) + 10
    if len(df_to_date) < min_bars:
        return None, {}

    latest       = df_to_date.iloc[-1]
    close_series = df_to_date["close"].dropna()
    close        = latest["close"]

    if pd.isna(close) or close == 0:
        return None, {}

    # ── Formula alias normalisation ──────────────────────────────────────────
    # Short names are canonical (matching scripts 16, 17 and strategies.json).
    # Long-form names accepted for backwards compatibility.
    _FORMULA_ALIASES = {
        "sma_distance": "sma_dist",
        "roc_weighted": "roc_weight",
    }
    formula = _FORMULA_ALIASES.get(formula, formula)

    # ── Formula dispatch ─────────────────────────────────────────────────────
    if formula == "sma_dist":
        if "sma_slow" not in df_to_date.columns:
            return None, {}
        sma_slow = latest["sma_slow"]
        if pd.isna(sma_slow) or sma_slow == 0:
            return None, {}
        momentum_score = ((close - sma_slow) / sma_slow) * 100

    elif formula == "roc_weight":
        period_list = list(active_roc_periods.values())
        if len(roc_weights) != len(period_list):
            roc_weights = [1.0 / len(period_list)] * len(period_list)
        weight_sum = sum(roc_weights)
        if weight_sum <= 0:
            return None, {}
        norm_weights = [w / weight_sum for w in roc_weights]
        weighted_roc = 0.0
        valid_terms  = 0
        for w, n_bars in zip(norm_weights, period_list):
            if len(close_series) > n_bars:
                past_close = close_series.iloc[-(n_bars + 1)]
                if past_close != 0:
                    weighted_roc += w * ((close / past_close) - 1)
                    valid_terms  += 1
        if valid_terms == 0:
            return None, {}
        momentum_score = weighted_roc * 100

    else:
        logger.error(f"{symbol}: Unknown momentum formula '{formula}'")
        return None, {}

    # ── Supplementary ROC metrics (always computed for both formulas) ─────────
    supplementary: Dict = {}
    for label, n_bars in active_roc_periods.items():
        if len(close_series) > n_bars:
            past_close = close_series.iloc[-(n_bars + 1)]
            supplementary[label] = (
                round(float(((close / past_close) - 1) * 100), 4)
                if past_close != 0 else None
            )
        else:
            supplementary[label] = None

    return float(momentum_score), supplementary


# ============================================================================
# MAIN RANKING FUNCTION
# ============================================================================

def rank_momentum(as_of_date: str, momentum_cfg: Optional[Dict] = None) -> Tuple[List[Dict], Dict]:
    """
    Calculate and rank momentum scores for all qualified instruments.

    Process:
        1. Load qualified_trends.json (Script 6 output)
        2. Load metadata from qualified_symbols.json (Script 4 output)
        3. For each qualified symbol:
            a. Load indicator parquet
            b. Calculate momentum score (primary formula)
            c. Compute supplementary ROC metrics
        4. Sort descending by momentum_score
        5. Assign ranks (1 = strongest momentum)

    Args:
        as_of_date:    Reference date (YYYY-MM-DD)
        momentum_cfg:  Dict with keys: formula, sma_period, roc_periods, roc_weights.
                       Defaults to sma_dist with module-level ROC_PERIODS.

    Returns:
        Tuple of:
            - ranked_list: List of dicts (sorted descending by momentum_score)
            - stats: Summary statistics dict
    """
    if momentum_cfg is None:
        momentum_cfg = {
            "formula":     "sma_dist",
            "roc_periods": list(ROC_PERIODS.values()),
            "roc_weights": [0.20, 0.30, 0.50],
        }

    active_formula     = momentum_cfg.get("formula",     "sma_dist")
    active_roc_periods = {f"roc_{n}d": n for n in momentum_cfg.get("roc_periods", list(ROC_PERIODS.values()))}
    active_roc_weights = momentum_cfg.get("roc_weights", [0.20, 0.30, 0.50])

    qualified_trends = load_qualified_trends(as_of_date)
    metadata         = load_qualified_metadata()

    total_qualified = len(qualified_trends)
    scored_count    = 0
    failed_count    = 0
    failed_symbols: List[str] = []
    results: List[Dict] = []

    logger.info(f"\nCalculating momentum scores for {total_qualified} qualified symbols...")

    for symbol, trend_data in qualified_trends.items():

        # Load indicator data
        df = load_indicator_data(symbol)
        if df is None:
            logger.warning(f"  SKIP {symbol}: no indicator file")
            failed_count += 1
            failed_symbols.append(symbol)
            continue

        # Calculate momentum score
        score, supplementary = calculate_momentum_score(
            df, as_of_date, symbol,
            formula=active_formula,
            active_roc_periods=active_roc_periods,
            roc_weights=active_roc_weights,
        )

        if score is None:
            logger.warning(f"  SKIP {symbol}: insufficient data for momentum calculation")
            failed_count += 1
            failed_symbols.append(symbol)
            continue

        # Sanity cap: scores above 500 almost always indicate an unadjusted
        # corporate action (reverse split) inflating the SMA baseline.
        # Flag for human review; still write to ranked output for transparency.
        MOMENTUM_SCORE_WARN_THRESHOLD = 500
        data_quality_flag = ''
        if score > MOMENTUM_SCORE_WARN_THRESHOLD:
            logger.warning(
                f"  FLAG {symbol}: momentum_score={score:.1f} exceeds "
                f"{MOMENTUM_SCORE_WARN_THRESHOLD} — likely unadjusted corporate "
                f"action. Included in ranked output but flagged. Verify price "
                f"history before trading."
            )
            data_quality_flag = 'extreme_momentum_score'

        # Pull metadata (name, sector, exchange, asset_class)
        meta = metadata.get(symbol, {})

        entry = {
            # -- Identification -----------------------------------------------
            'symbol':       symbol,
            'name':         meta.get('name', ''),
            'exchange':     meta.get('exchange', trend_data.get('exchange', '')),
            'sector':       meta.get('sector', ''),
            'asset_class':  meta.get('instrument_type', ''),

            # -- Primary ranking score ----------------------------------------
            'momentum_score': round(score, 4),
            # rank assigned after sorting

            # -- Trend qualification snapshot (from Script 6) -----------------
            'close':    trend_data['close'],
            'sma_fast': trend_data['sma_fast'],
            'sma_slow': trend_data['sma_slow'],
            'adx':      trend_data['adx'],
            'atr_pct':  trend_data['atr_pct'],

            # -- Supplementary ROC metrics (transparency, not for ranking) -----
            'roc_20d':  supplementary.get('roc_20d'),
            'roc_60d':  supplementary.get('roc_60d'),
            'roc_120d': supplementary.get('roc_120d'),

            # -- Audit trail --------------------------------------------------
            'as_of_date':        as_of_date,
            'scoring_formula':   active_formula,
            'data_quality_flag': data_quality_flag,  # '' = clean
        }

        results.append(entry)
        scored_count += 1

    # -------------------------------------------------------------------------
    # Sort descending by momentum_score — deterministic tie-break by symbol
    # -------------------------------------------------------------------------
    results.sort(key=lambda x: (-x['momentum_score'], x['symbol']))

    # Assign integer ranks (1-based)
    for rank_idx, entry in enumerate(results, start=1):
        entry['rank'] = rank_idx

    # -------------------------------------------------------------------------
    # Summary statistics
    # -------------------------------------------------------------------------
    scores = [r['momentum_score'] for r in results]
    stats = {
        'as_of_date':            as_of_date,
        'total_qualified':       total_qualified,
        'total_scored':          scored_count,
        'total_failed':          failed_count,
        'failed_symbols':        failed_symbols,
        'score_max':             round(max(scores), 4)              if scores else None,
        'score_min':             round(min(scores), 4)              if scores else None,
        'score_mean':            round(float(np.mean(scores)), 4)   if scores else None,
        'score_median':          round(float(np.median(scores)), 4) if scores else None,
        'score_std':             round(float(np.std(scores)), 4)    if scores else None,
        'positive_momentum_pct': round(
            sum(1 for s in scores if s > 0) / len(scores) * 100, 1
        ) if scores else 0,
        'scoring_formula':  active_formula,
        'generated_at':     datetime.now().isoformat(),
    }

    return results, stats


# ============================================================================
# OUTPUT FUNCTIONS
# ============================================================================

def save_momentum_ranked(ranked_list: List[Dict], output_file: Path) -> None:
    """
    Persist the full ranked list to JSON.

    Format:
        {
            "metadata": { "as_of_date": ..., "total_ranked": ..., "scoring_formula": ... },
            "ranked": [ { rank, symbol, momentum_score, ... }, ... ]
        }
    """
    output_file.parent.mkdir(parents=True, exist_ok=True)

    scoring_formula = ranked_list[0].get('scoring_formula', 'sma_dist') if ranked_list else 'sma_dist'
    output = {
        "metadata": {
            "as_of_date":      ranked_list[0]['as_of_date'] if ranked_list else None,
            "total_ranked":    len(ranked_list),
            "scoring_formula": scoring_formula,
            "generated_at":    datetime.now().isoformat(),
        },
        "ranked": ranked_list,
    }

    with open(output_file, 'w') as f:
        json.dump(output, f, indent=2, default=str)

    logger.info(f"Saved {len(ranked_list)} ranked entries -> {output_file}")


def save_momentum_summary(stats: Dict, output_file: Path) -> None:
    """Save run statistics to JSON"""
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with open(output_file, 'w') as f:
        json.dump(stats, f, indent=2, default=str)

    logger.info(f"Saved momentum summary -> {output_file}")


def save_momentum_csv(ranked_list: List[Dict], output_file: Path) -> None:
    """
    Save human-readable CSV report.

    Columns:
        rank, symbol, name, exchange, sector, asset_class,
        momentum_score, close, sma_fast, sma_slow, adx, atr_pct,
        roc_20d, roc_60d, roc_120d, as_of_date, scoring_formula
    """
    output_file.parent.mkdir(parents=True, exist_ok=True)

    if not ranked_list:
        logger.warning("Empty ranked list — skipping CSV export")
        return

    df = pd.DataFrame(ranked_list)

    col_order = [
        'rank', 'symbol', 'name', 'exchange', 'sector', 'asset_class',
        'momentum_score',
        'close', 'sma_fast', 'sma_slow', 'adx', 'atr_pct',
        'roc_20d', 'roc_60d', 'roc_120d',
        'as_of_date', 'scoring_formula',
    ]
    available_cols = [c for c in col_order if c in df.columns]
    df = df[available_cols]

    df.to_csv(output_file, index=False, float_format='%.4f')
    logger.info(f"Saved CSV report -> {output_file}")


def print_top_n_summary(ranked_list: List[Dict], n: int = 20) -> None:
    """Print a formatted top-N ranking table to console"""
    top_n = ranked_list[:n]

    as_of = ranked_list[0]['as_of_date'] if ranked_list else 'N/A'
    logger.info(f"\n{'='*80}")
    logger.info(f"TOP {n} BY MOMENTUM SCORE  (as of {as_of})")
    logger.info(f"{'='*80}")
    header = (
        f"{'Rank':>4}  {'Symbol':<16} {'Score':>8}  "
        f"{'Close':>8}  {'SMA_slow':>8}  {'ADX':>6}  {'ATR%':>6}  "
        f"{'ROC20':>7}  {'ROC60':>7}"
    )
    logger.info(header)
    logger.info("-" * 80)

    for entry in top_n:
        roc20 = f"{entry['roc_20d']:+.1f}%" if entry.get('roc_20d') is not None else "  N/A  "
        roc60 = f"{entry['roc_60d']:+.1f}%" if entry.get('roc_60d') is not None else "  N/A  "
        line = (
            f"{entry['rank']:>4}  {entry['symbol']:<16} "
            f"{entry['momentum_score']:>+8.2f}  "
            f"{entry['close']:>8.2f}  {entry['sma_slow']:>8.2f}  "
            f"{entry['adx']:>6.1f}  {entry['atr_pct']:>6.2f}  "
            f"{roc20:>7}  {roc60:>7}"
        )
        logger.info(line)

    if len(ranked_list) > n:
        logger.info(f"\n  ... and {len(ranked_list) - n} more (see full CSV report)")


# ============================================================================
# ARGUMENT PARSING
# ============================================================================

def parse_arguments() -> argparse.Namespace:
    """Parse and validate command-line arguments"""
    parser = argparse.ArgumentParser(
        description='Momentum Ranker — Script 7 (v3.9)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Rank qualified universe as of month-end (all active strategies)
    python scripts/07_rank_momentum.py --as-of-date 2026-01-31

    # Single strategy
    python scripts/07_rank_momentum.py --as-of-date 2026-01-31 --strategy sma_dist
    python scripts/07_rank_momentum.py --as-of-date 2026-01-31 --strategy roc_weight

    # Preview top 30 in console
    python scripts/07_rank_momentum.py --as-of-date 2026-01-31 --top 30

    # Dry run — compute scores but do not write output files
    python scripts/07_rank_momentum.py --as-of-date 2026-01-31 --dry-run

Dependencies (must be run first, in order):
    01_download_eodhd_bulk.py
    02_download_yahoo_fundamentals.py
    03_consolidate_validate_data.py
    04_screen_universe.py
    05_calculate_indicators.py
    06_qualify_trends.py
    07_rank_momentum.py  <- THIS SCRIPT
        """
    )

    parser.add_argument(
        '--as-of-date',
        required=True,
        metavar='YYYY-MM-DD',
        help='Reference date for momentum calculation',
    )
    parser.add_argument(
        '--top',
        type=int,
        default=20,
        metavar='N',
        help='Number of top entries to display in console summary (default: 20)',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Compute scores but skip writing output files',
    )
    parser.add_argument(
        '--config',
        metavar='PATH',
        default=None,
        help='Path to a strategy parameters JSON (legacy experiment mode).',
    )
    parser.add_argument(
        '--output-dir',
        metavar='PATH',
        dest='output_dir',
        default=None,
        help='Output directory (legacy experiment mode).',
    )
    add_strategy_argument(parser)

    return parser.parse_args()


# ============================================================================
# PER-STRATEGY RUNNER
# ============================================================================

def _run_for_strategy(
    strategy:   "StrategyDef",
    as_of_date: str,
    top:        int,
    dry_run:    bool,
) -> int:
    """Run momentum ranking for one strategy and write namespaced outputs."""
    if not strategy.config_path.exists():
        logger.error(f"[{strategy.name}] Config not found: {strategy.config_path}")
        return 1
    try:
        momentum_cfg = _load_experiment_momentum_config(str(strategy.config_path))
    except Exception as exc:
        logger.error(f"[{strategy.name}] Failed to load config: {exc}")
        return 1

    strat_signals = strategy.signals_dir(DATA_CACHE_DIR)
    strat_signals.mkdir(parents=True, exist_ok=True)
    strat_reports = strategy.reports_dir(PROJECT_ROOT, "signals")
    strat_reports.mkdir(parents=True, exist_ok=True)

    logger.info(f"\n[{strategy.name}] -- {strategy.label} ({'LIVE' if strategy.deployed else 'PAPER'}) --")
    logger.info(f"[{strategy.name}] Formula : {momentum_cfg.get('formula', 'sma_dist')}")
    logger.info(f"[{strategy.name}] Output  : {strat_signals}")

    try:
        ranked_list, stats = rank_momentum(as_of_date, momentum_cfg=momentum_cfg)
    except Exception as e:
        logger.error(f"[{strategy.name}] Fatal error: {e}", exc_info=True)
        return 1

    if not ranked_list:
        logger.error(f"[{strategy.name}] No instruments scored.")
        return 1

    print_top_n_summary(ranked_list, n=top)

    if dry_run:
        logger.info(f"[{strategy.name}] Dry-run — outputs NOT written")
        return 0

    date_tag = as_of_date.replace("-", "")
    try:
        save_momentum_ranked(ranked_list, strat_signals / "momentum_ranked.json")
        save_momentum_summary(stats,      strat_signals / "momentum_summary.json")
        save_momentum_csv(ranked_list,    strat_reports / f"{date_tag}_momentum_ranked.csv")
    except Exception as e:
        logger.error(f"[{strategy.name}] Error writing outputs: {e}", exc_info=True)
        return 1

    logger.info(f"[{strategy.name}] Ranked {stats['total_scored']:,} -> {strat_signals}")
    return 0


# ============================================================================
# MAIN
# ============================================================================

def main() -> int:
    start_time = datetime.now()
    logger.info("=" * 70)
    logger.info("MOMENTUM RANKER -- Script 7")
    logger.info("Architecture v3.9 (Mar 2026)")
    logger.info("=" * 70)

    args       = parse_arguments()
    as_of_date = args.as_of_date

    try:
        datetime.strptime(as_of_date, "%Y-%m-%d")
    except ValueError:
        logger.error(f"Invalid date format: '{as_of_date}'. Expected YYYY-MM-DD")
        return 1

    logger.info(f"Evaluation date : {as_of_date}")
    logger.info(f"Dry run         : {args.dry_run}")

    # ── Resolve strategies ────────────────────────────────────────────────────
    try:
        strategies = resolve_strategies(
            strategy_arg=args.strategy,
            project_root=PROJECT_ROOT,
        )
    except (FileNotFoundError, ValueError) as exc:
        logger.error(f"Strategy resolution failed: {exc}")
        return 1

    logger.info(f"Strategies      : {[s.name for s in strategies]}")

    failed: list = []
    for strategy in strategies:
        rc = _run_for_strategy(
            strategy=strategy,
            as_of_date=as_of_date,
            top=args.top,
            dry_run=args.dry_run,
        )
        if rc != 0:
            failed.append(strategy.name)

    elapsed = datetime.now() - start_time
    logger.info(f"\n{'='*70}")
    logger.info("MOMENTUM RANKING COMPLETE")
    logger.info(f"{'='*70}")
    logger.info(f"  Duration   : {elapsed}")
    logger.info(f"  Strategies : {len(strategies)} requested, {len(failed)} failed")
    if failed:
        logger.error(f"  FAILED     : {failed}")
        return 1
    logger.info(f"  Next step  : python scripts/08_calculate_stops.py --as-of-date {as_of_date}")
    logger.info("=" * 70)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.warning("\n\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unhandled exception: {e}", exc_info=True)
        sys.exit(1)
