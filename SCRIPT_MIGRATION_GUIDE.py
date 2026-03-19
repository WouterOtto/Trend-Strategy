#!/usr/bin/env python3
"""
SCRIPT_MIGRATION_GUIDE.py
==========================
Reference guide for migrating all hardcoded parameters to config/params.py.

For each script that requires changes, this file shows:
  1. The OLD hardcoded block to DELETE
  2. The NEW import + replacement to INSERT
  3. All in-body references to update

Run this file to print a summary of all affected scripts:
    python SCRIPT_MIGRATION_GUIDE.py

Architecture: v3.8 (Mar 2026)
"""

MIGRATION_PLAN = {

    # ═══════════════════════════════════════════════════════════════════════
    # SCRIPT 05 — calculate_indicators.py
    # ═══════════════════════════════════════════════════════════════════════
    "05_calculate_indicators.py": {
        "priority": "HIGH",
        "reason": "sma_fast=50 and sma_slow=200 contradict Script 16 DEFAULTS (100/250). "
                  "These drive indicator computation for live pipeline (Scripts 6,7,8).",
        "inconsistencies_fixed": [
            "sma_fast: 50 → 100  (matches WFO-validated Script 16 value)",
            "sma_slow: 200 → 250 (matches WFO-validated Script 16 value)",
            "atr_period: already 20, confirmed correct",
            "adx_period: already 14, confirmed correct",
            "MIN_HISTORY_DAYS: 252, confirmed correct",
            "MIN_VALID_INDICATORS: 20, confirmed correct",
        ],
        "delete_block": """
# Indicator parameters (from Architecture v3.2)
INDICATOR_PARAMS = {
    'sma_fast': 50,      # Fast moving average period
    'sma_slow': 200,     # Slow moving average period
    'atr_period': 20,    # Average True Range period
    'adx_period': 14,    # Average Directional Index period
}

MIN_HISTORY_DAYS = 252
MIN_VALID_INDICATORS = 20
""",
        "insert_block": """
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.params import P

INDICATOR_PARAMS = {
    'sma_fast':   P.indicators.sma_fast,
    'sma_slow':   P.indicators.sma_slow,
    'atr_period': P.indicators.atr_period,
    'adx_period': P.indicators.adx_period,
}

MIN_HISTORY_DAYS     = P.trend_qualification.min_history_days
MIN_VALID_INDICATORS = P.trend_qualification.min_valid_indicators
""",
        "body_changes": [],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # SCRIPT 06 — qualify_trends.py
    # ═══════════════════════════════════════════════════════════════════════
    "06_qualify_trends.py": {
        "priority": "HIGH",
        "reason": "DEFAULT_ADX_THRESHOLD=20 may conflict with WFO-optimized adx_threshold=15. "
                  "Must be consistent with Script 16 DEFAULTS and Script 10 exit logic.",
        "inconsistencies_fixed": [
            "DEFAULT_ADX_THRESHOLD: 20 → driven by P.trend_qualification.adx_threshold (15)",
            "DEFAULT_MIN_DATA_POINTS: 200 → P.trend_qualification.min_data_points (250, matches sma_slow)",
        ],
        "delete_block": """
DEFAULT_ADX_THRESHOLD = 20          # ADX > 20 = trending (Wilder standard)
DEFAULT_MIN_DATA_POINTS = 200       # Minimum rows needed to have valid SMA_200
""",
        "insert_block": """
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.params import P

DEFAULT_ADX_THRESHOLD   = P.trend_qualification.adx_threshold
DEFAULT_MIN_DATA_POINTS = P.trend_qualification.min_data_points
""",
        "body_changes": [
            "load_strategy_parameters() function: replace JSON read fallback default values "
            "with P.trend_qualification.adx_threshold etc. so CLI --min-adx still overrides.",
        ],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # SCRIPT 07 — rank_momentum.py
    # ═══════════════════════════════════════════════════════════════════════
    "07_rank_momentum.py": {
        "priority": "HIGH",
        "reason": "MOMENTUM_SMA_PERIOD=200 hardcoded. Architecture sma_slow=250. "
                  "Momentum score uses (Close - SMA_slow)/SMA_slow — must match sma_slow.",
        "inconsistencies_fixed": [
            "MOMENTUM_SMA_PERIOD: 200 → P.momentum.sma_period (250) — formula now consistent "
            "with sma_slow used in trend qualification",
            "ROC_PERIODS dict: replace hardcoded 20/60/120 with P.momentum.roc_periods",
        ],
        "delete_block": """
MOMENTUM_SMA_PERIOD = 200   # Primary ranking formula uses SMA_200 deviation

ROC_PERIODS = {
    "roc_20d":  20,
    "roc_60d":  60,
    "roc_120d": 120,
}

MIN_BARS_REQUIRED = MOMENTUM_SMA_PERIOD + ROC_PERIODS["roc_120d"] + 10  # buffer
""",
        "insert_block": """
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.params import P

MOMENTUM_SMA_PERIOD = P.momentum.sma_period

ROC_PERIODS = {
    f"roc_{n}d": n for n in P.momentum.roc_periods
}

MIN_BARS_REQUIRED = MOMENTUM_SMA_PERIOD + max(P.momentum.roc_periods) + 10
""",
        "body_changes": [],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # SCRIPT 08 — calculate_stops.py
    # ═══════════════════════════════════════════════════════════════════════
    "08_calculate_stops.py": {
        "priority": "HIGH",
        "reason": "init_stop_mult=3.0 and trail_activation=0.15 contradict Script 16 "
                  "WFO-validated values (2.5 and 0.08). Stop prices written by this script "
                  "are consumed directly by live trading in Script 11.",
        "inconsistencies_fixed": [
            "INITIAL_STOP_MULTIPLIER: 3.0 → P.stops.init_stop_mult (2.5)",
            "TRAILING_STOP_MULTIPLIER: 4.0 → P.stops.trail_stop_mult (3.5)",
            "TRAILING_ACTIVATION_PCT: 0.15 → P.stops.trail_activation (0.08)",
        ],
        "delete_block": """
INITIAL_STOP_MULTIPLIER: float = 3.0   # Initial_Stop = Entry - (3.0 × ATR)
TRAILING_STOP_MULTIPLIER: float = 4.0  # Trailing_Stop = Close - (4.0 × ATR)
TRAILING_ACTIVATION_PCT: float  = 0.15  # 15% profit required to activate
""",
        "insert_block": """
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.params import P

INITIAL_STOP_MULTIPLIER:  float = P.stops.init_stop_mult
TRAILING_STOP_MULTIPLIER: float = P.stops.trail_stop_mult
TRAILING_ACTIVATION_PCT:  float = P.stops.trail_activation
MAX_STOP_DISTANCE_PCT:    float = P.stops.max_stop_distance_pct
""",
        "body_changes": [
            "load_strategy_parameters(): replace JSON read fallback values with P.stops.*",
        ],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # SCRIPT 09 — calculate_position_sizes.py
    # ═══════════════════════════════════════════════════════════════════════
    "09_calculate_position_sizes.py": {
        "priority": "HIGH",
        "reason": "9 module-level constants all scattered. POSITION_COUNT_SCHEDULE caps at 25 "
                  "while WFO result is max_positions=40. Must unify.",
        "inconsistencies_fixed": [
            "POSITION_COUNT_SCHEDULE: old max was 25 for accounts >= 100k → WFO result is 40",
            "DEFAULT_MAX_POSITIONS: 25 → driven by schedule for given equity",
            "All other constants: confirmed consistent, now centralized",
        ],
        "delete_block": """
TARGET_RISK_PER_POSITION: float = 0.02
MIN_POSITION_PCT:          float = 0.005
MAX_POSITION_PCT:          float = 0.08
ENTRY_LIMIT_OFFSET:        float = 0.005
MAX_CRYPTO_ALLOCATION_PCT: float = 0.20
MAX_SECTOR_ALLOCATION_PCT: float = 0.30
MIN_CASH_RESERVE_PCT:      float = 0.05
MAX_TOP3_CONCENTRATION_PCT: float = 0.30
MAX_STOP_DISTANCE_PCT:     float = 0.40
POSITION_COUNT_SCHEDULE = [
    (25_000,  10),
    (50_000,  15),
    (100_000, 20),
]
DEFAULT_MAX_POSITIONS = 25
""",
        "insert_block": """
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.params import P

TARGET_RISK_PER_POSITION:  float = P.position_sizing.risk_per_trade
MIN_POSITION_PCT:          float = P.position_sizing.pos_floor_pct
MAX_POSITION_PCT:          float = P.position_sizing.pos_ceil_pct
ENTRY_LIMIT_OFFSET:        float = P.position_sizing.entry_limit_offset
MAX_CRYPTO_ALLOCATION_PCT: float = P.portfolio_constraints.max_crypto_pct
MAX_SECTOR_ALLOCATION_PCT: float = P.portfolio_constraints.max_sector_pct
MIN_CASH_RESERVE_PCT:      float = P.portfolio_constraints.min_cash_pct
MAX_TOP3_CONCENTRATION_PCT:float = P.portfolio_constraints.max_top3_concentration_pct
MAX_STOP_DISTANCE_PCT:     float = P.stops.max_stop_distance_pct
""",
        "body_changes": [
            "get_max_positions(equity): replace inline POSITION_COUNT_SCHEDULE with "
            "P.position_sizing.max_positions_for_equity(equity)",
        ],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # SCRIPT 10 — generate_exit_signals.py
    # ═══════════════════════════════════════════════════════════════════════
    "10_generate_exit_signals.py": {
        "priority": "HIGH",
        "reason": "SMA_FAST_PERIOD=50, SMA_SLOW_PERIOD=200 hardcoded. Script 16 uses 100/250. "
                  "Exit rule 2 (death cross) uses these periods — mismatched indicators "
                  "will fire exit signals on different data than trend entry uses.",
        "inconsistencies_fixed": [
            "SMA_FAST_PERIOD: 50 → P.indicators.sma_fast (100)",
            "SMA_SLOW_PERIOD: 200 → P.indicators.sma_slow (250)",
            "ADX_WEAKNESS_THRESHOLD: 15.0 → P.trend_qualification.adx_weak (10)",
            "ADX_CONSECUTIVE_DAYS: 3, confirmed correct → P.trend_qualification.adx_weakness_days",
            "MAX_DATA_STALENESS_DAYS: 3 → P.circuit_breakers.cb_data_staleness_days",
        ],
        "delete_block": """
SMA_FAST_PERIOD: int = 50    # SMA_50
SMA_SLOW_PERIOD: int = 200   # SMA_200
ADX_PERIOD: int         = 14
ADX_WEAKNESS_THRESHOLD: float = 15.0
ADX_CONSECUTIVE_DAYS: int     = 3
MAX_DATA_STALENESS_DAYS: int = 3
""",
        "insert_block": """
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.params import P

SMA_FAST_PERIOD:         int   = P.indicators.sma_fast
SMA_SLOW_PERIOD:         int   = P.indicators.sma_slow
ADX_PERIOD:              int   = P.indicators.adx_period
ADX_WEAKNESS_THRESHOLD:  float = P.trend_qualification.adx_weak
ADX_CONSECUTIVE_DAYS:    int   = P.trend_qualification.adx_weakness_days
MAX_DATA_STALENESS_DAYS: int   = P.circuit_breakers.cb_data_staleness_days
""",
        "body_changes": [
            "load_strategy_parameters(): replace JSON read fallback default values with P.*",
            "Column references to 'SMA_50' / 'SMA_200' in indicator parquet files must use "
            "f'SMA_{P.indicators.sma_fast}' and f'SMA_{P.indicators.sma_slow}' — or keep "
            "fixed column name strings if Script 05 writes fixed names regardless of period.",
        ],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # SCRIPT 11 — monthly_rebalancing.py
    # ═══════════════════════════════════════════════════════════════════════
    "11_monthly_rebalancing.py": {
        "priority": "HIGH",
        "reason": "CB_MAX_DRAWDOWN_PCT=-0.15 is the live circuit-breaker; Script 16 uses -0.30 "
                  "for backtest halt. These have different semantics — both are now explicitly "
                  "named in strategy_parameters.json. POSITION_COUNT_SCHEDULE capped at 25.",
        "inconsistencies_fixed": [
            "CB_MAX_DRAWDOWN_PCT: -0.15 → P.circuit_breakers.cb_drawdown_warn (live halt for entries)",
            "POSITION_COUNT_SCHEDULE: max 25 → schedule-driven via P.position_sizing",
            "CB_VIX_HALT_LEVEL, CB_VIX_RESUME_LEVEL: confirmed values now centralized",
            "CB_MAX_CORRELATION, CB_MAX_TOP3_CONCENTRATION: confirmed values now centralized",
        ],
        "delete_block": """
POSITION_COUNT_SCHEDULE: List[Tuple[float, int]] = [
    (25_000,  10),
    (50_000,  15),
    (100_000, 20),
    (float("inf"), 25),
]
ASSET_CLASS_TARGETS = {
    "etf":    {"min": 0,  "target": 38, "max": 45},
    "stock":  {"min": 0,  "target": 42, "max": 50},
    "crypto": {"min": 0,  "target": 15, "max": 20},
}
ALLOCATION_TOLERANCE = 10.0
CB_MAX_DRAWDOWN_PCT       = -0.15
CB_VIX_HALT_LEVEL         = 40.0
CB_VIX_RESUME_LEVEL       = 30.0
CB_MAX_CORRELATION        = 0.85
CB_MAX_TOP3_CONCENTRATION = 0.30
CB_MAX_DATA_STALENESS_DAYS = 3
""",
        "insert_block": """
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.params import P

CB_MAX_DRAWDOWN_PCT        = P.circuit_breakers.cb_drawdown_warn      # entry halt threshold
CB_VIX_HALT_LEVEL          = P.circuit_breakers.cb_vix_enter
CB_VIX_RESUME_LEVEL        = P.circuit_breakers.cb_vix_resume
CB_MAX_CORRELATION         = P.portfolio_constraints.max_pairwise_correlation
CB_MAX_TOP3_CONCENTRATION  = P.portfolio_constraints.max_top3_concentration_pct
CB_MAX_DATA_STALENESS_DAYS = P.circuit_breakers.cb_data_staleness_days
MAX_CRYPTO_PCT             = P.portfolio_constraints.max_crypto_pct

# NOTE: ASSET_CLASS_TARGETS and ALLOCATION_TOLERANCE are portfolio
# construction preferences, not risk parameters — keep in strategy_parameters.json
# under a new "asset_class_targets" section if rebalancing guidance is needed.
# They are NOT moved yet; they remain local constants until architecture
# sign-off on target allocations.
""",
        "body_changes": [
            "get_max_positions(equity): replace inline schedule with "
            "P.position_sizing.max_positions_for_equity(equity)",
            "load_strategy_parameters(): replace JSON read fallback default values with P.*",
        ],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # SCRIPT 14 — daily_monitoring.py
    # ═══════════════════════════════════════════════════════════════════════
    "14_daily_monitoring.py": {
        "priority": "MEDIUM",
        "reason": "10 module-level constants. ADX_WEAKNESS_EXIT=15 conflicts with Script 16 "
                  "adx_weak=10. Monitoring thresholds must match live trading logic exactly.",
        "inconsistencies_fixed": [
            "ADX_WEAKNESS_EXIT: 15.0 → P.trend_qualification.adx_weak (10)",
            "ADX_DETERIORATION_WARN: 20.0 → P.trend_qualification.adx_threshold (15 or 20)",
            "CB_MAX_DRAWDOWN_CRITICAL: -0.15 → P.circuit_breakers.cb_drawdown_warn",
            "CB_MAX_DRAWDOWN_WARNING: -0.10 → P.circuit_breakers.cb_drawdown_watch",
            "CB_VIX_HALT_LEVEL: 40.0 → P.circuit_breakers.cb_vix_enter",
            "CB_VIX_WARN_LEVEL: 30.0 → P.circuit_breakers.cb_vix_resume",
            "CORRELATION_LOOKBACK_DAYS: 60 → P.portfolio_constraints.correlation_lookback_days",
        ],
        "delete_block": """
CB_MAX_DRAWDOWN_CRITICAL    = -0.15
CB_MAX_DRAWDOWN_WARNING     = -0.10
CB_MAX_TOP3_CONCENTRATION   = 0.30
CB_MAX_SINGLE_POSITION_PCT  = 0.10
CB_MAX_PAIRWISE_CORRELATION = 0.85
CB_MAX_DATA_STALENESS_DAYS  = 3
CB_VIX_HALT_LEVEL           = 40.0
CB_VIX_WARN_LEVEL           = 30.0
STOP_PROXIMITY_HIGH_PCT    = 0.03
STOP_PROXIMITY_MEDIUM_PCT  = 0.05
ADX_WEAKNESS_EXIT      = 15.0
ADX_DETERIORATION_WARN = 20.0
ADX_CONSECUTIVE_DAYS   = 3
CORRELATION_LOOKBACK_DAYS = 60
""",
        "insert_block": """
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.params import P

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
""",
        "body_changes": [],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # SCRIPT 16 — backtest_engine.py
    # ═══════════════════════════════════════════════════════════════════════
    "16_backtest_engine.py": {
        "priority": "HIGH",
        "reason": "DEFAULTS dict is the authoritative source Script 17 imports. "
                  "Replace dict values with P.as_backtest_defaults() so all values "
                  "flow from one config. Keep DEFAULTS as a module-level dict for "
                  "backward compatibility with Script 17 import pattern.",
        "inconsistencies_fixed": [
            "adx_weak: was 10, confirmed consistent with P.trend_qualification.adx_weak",
            "trail_activation: was 0.08, confirmed consistent",
            "max_positions: was 40 from WFO result, now driven by schedule",
            "cb_drawdown: -0.30 is backtest-simulation halt — DIFFERENT from live cb_drawdown_warn (-0.15); "
            "mapped to P.circuit_breakers.cb_drawdown_halt",
        ],
        "delete_block": """
DEFAULTS = dict(
    sma_fast           = 100,
    sma_slow           = 250,
    adx_threshold      = 20,
    adx_weak           = 10,
    adx_weakness_days  = 3,
    momentum_period    = 200,
    init_stop_mult     = 2.5,
    trail_stop_mult    = 3.5,
    trail_activation   = 0.08,
    max_positions      = 40,
    risk_per_trade     = 0.02,
    pos_floor_pct      = 0.005,
    pos_ceil_pct       = 0.08,
    limit_slip         = 0.005,
    limit_cancel_days  = 2,
    slippage_stock     = 0.0005,
    slippage_crypto    = 0.001,
    cost_bps           = 10,
    initial_equity     = 50_000.0,
    cb_drawdown        = -0.30,
    cb_recovery_pct    = 0.05,
    cb_min_halt_days   = 30,
    cb_vix_enter       = 40,
    cb_vix_resume      = 30,
    cb_vix_resume_days = 3,
)
""",
        "insert_block": """
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.params import P

# DEFAULTS remains a plain dict for backward compatibility with Script 17's
# `from backtest_engine_16 import DEFAULTS` import pattern.
DEFAULTS = P.as_backtest_defaults()
""",
        "body_changes": [
            "No body changes required. CLI argparse defaults that reference DEFAULTS[key] "
            "will automatically pick up the centralized values.",
        ],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # SCRIPT 17 — walk_forward_optimizer.py
    # ═══════════════════════════════════════════════════════════════════════
    "17_walk_forward_optimizer.py": {
        "priority": "HIGH",
        "reason": "PARAM_GRID, PARAM_GRID_FAST, FIXED_PARAMS, WFO_* window constants, "
                  "and all STABILITY_* thresholds hardcoded. Most numerous set of "
                  "inline constants in the codebase.",
        "inconsistencies_fixed": [
            "All WFO grid evolution is now tracked in strategy_parameters.json _comment fields",
            "FIXED_PARAMS.adx_threshold: was 15 (commented out in grid) — explicit in fixed_params",
        ],
        "delete_block": """
PARAM_GRID = {
    "sma_slow":          [200, 250, 300],
    "init_stop_mult":    [2.0, 2.5, 3.0, 3.5],
    "trail_stop_mult":   [3.0, 3.5, 4.0, 4.5],
    "max_positions":     [30, 35, 40, 45],
}
PARAM_GRID_FAST = {
    "sma_slow":          [200, 250, 300],
    "trail_stop_mult":   [3.5, 4.0, 4.5],
    "max_positions":     [20, 25],
}
FIXED_PARAMS = {
    "sma_fast":          100,
    "adx_threshold":     15,
    "init_stop_mult":    2.5,
    "trail_activation":  0.15,
    "risk_per_trade":    0.02,
    "cost_bps":          10,
    "pos_floor_pct":     0.005,
    "pos_ceil_pct":      0.08,
}
WFO_IS_MONTHS   = 24
WFO_OOS_MONTHS  = 12
WFO_ROLL_MONTHS = 6
STABILITY_EXCELLENT   = 0.8
STABILITY_GOOD        = 0.7
STABILITY_ACCEPTABLE  = 0.6
OOS_CONSISTENCY_PASS  = 0.70
OOS_CONSISTENCY_WARN  = 0.60
PARAM_CV_EXCELLENT    = 0.10
PARAM_CV_GOOD         = 0.20
""",
        "insert_block": """
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.params import P

PARAM_GRID      = P.optimization_grid
PARAM_GRID_FAST = P.optimization_grid_fast
FIXED_PARAMS    = P.fixed_params

WFO_IS_MONTHS   = P.walk_forward.is_months
WFO_OOS_MONTHS  = P.walk_forward.oos_months
WFO_ROLL_MONTHS = P.walk_forward.roll_months

STABILITY_EXCELLENT  = P.walk_forward.stability_excellent
STABILITY_GOOD       = P.walk_forward.stability_good
STABILITY_ACCEPTABLE = P.walk_forward.stability_acceptable
OOS_CONSISTENCY_PASS = P.walk_forward.oos_consistency_pass
OOS_CONSISTENCY_WARN = P.walk_forward.oos_consistency_warn
PARAM_CV_EXCELLENT   = P.walk_forward.param_cv_excellent
PARAM_CV_GOOD        = P.walk_forward.param_cv_good
""",
        "body_changes": [
            "argparse defaults for --is-months, --oos-months, --roll-months already read from "
            "WFO_IS_MONTHS etc. — no body change needed after constants update.",
            "INDICATOR_PARAMS frozenset: still correctly set to {'sma_fast', 'sma_slow'} — no change.",
        ],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # SCRIPT 18 — monte_carlo_simulator.py
    # ═══════════════════════════════════════════════════════════════════════
    "18_monte_carlo_simulator.py": {
        "priority": "MEDIUM",
        "reason": "DEFAULTS and GATES dicts hardcoded. Risk-free rate duplicated vs "
                  "backtesting section. Pass/fail gates scattered from validation logic.",
        "inconsistencies_fixed": [
            "risk_free_rate: 0.05, confirmed consistent — now sourced from monte_carlo section",
            "All gate values: now centralized and version-controlled",
        ],
        "delete_block": """
DEFAULTS = dict(
    n_paths         = 10_000,
    block_length    = 21,
    risk_free_rate  = 0.05,
    trading_days    = 252,
    soft_ruin_level = 0.50,
    hard_dd_limit   = 0.40,
    seed            = 42,
)
GATES = dict(
    mc1_median_cagr_min       =  0.00,
    mc2_p5_equity_pct_min     =  0.50,
    mc3_p95_maxdd_max         =  0.50,
    mc4_median_sharpe_min     =  0.30,
    mc5_ruin_prob_max         =  0.05,
    mc6_m3_ruin_prob_max      =  0.10,
)
REPORT_PERCENTILES = [1, 5, 10, 25, 50, 75, 90, 95, 99]
""",
        "insert_block": """
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.params import P

mc = P.monte_carlo
DEFAULTS = dict(
    n_paths         = mc.n_paths,
    block_length    = mc.block_length,
    risk_free_rate  = mc.risk_free_rate,
    trading_days    = mc.trading_days_per_year,
    soft_ruin_level = mc.soft_ruin_level,
    hard_dd_limit   = mc.hard_dd_limit,
    seed            = mc.seed,
)
GATES = dict(
    mc1_median_cagr_min    = mc.gates.mc1_median_cagr_min,
    mc2_p5_equity_pct_min  = mc.gates.mc2_p5_equity_pct_min,
    mc3_p95_maxdd_max      = mc.gates.mc3_p95_maxdd_max,
    mc4_median_sharpe_min  = mc.gates.mc4_median_sharpe_min,
    mc5_ruin_prob_max      = mc.gates.mc5_ruin_prob_max,
    mc6_m3_ruin_prob_max   = mc.gates.mc6_m3_ruin_prob_max,
)
REPORT_PERCENTILES = mc.report_percentiles

# Fast mode uses reduced path count
FAST_N_PATHS = mc.n_paths_fast
""",
        "body_changes": [
            "parse_arguments(): --fast-mode path count: replace hardcoded 2_000 with FAST_N_PATHS",
        ],
    },
}

# ═══════════════════════════════════════════════════════════════════════════
# SCRIPTS WITH NO CHANGES REQUIRED
# ═══════════════════════════════════════════════════════════════════════════

NO_CHANGE_SCRIPTS = {
    "00_trend_strategy_pipeline.py": "Orchestrator only. Passes CLI args to subscripts.",
    "01_download_eodhd_bulk.py":     "No strategy parameters. API config only.",
    "02_download_yahoo_fundamentals.py": "No strategy parameters. API/metadata only.",
    "03_consolidate_validate_data.py": "Data pipeline only. No strategy parameters.",
    "04_screen_universe.py":         "Already reads config/filter_thresholds.json correctly.",
    "12_generate_recommendation_report.py": "Report formatter. Reads upstream JSON outputs.",
    "13_log_execution.py":           "Audit logger. No strategy parameters.",
    "15_generate_technical_charts.py": "Visualization. Reads upstream data, no params.",
    "19_backtest_validator.py":       "Reads backtest results, thresholds passed from Script 16.",
    "20_oos_validator.py":            "Reads WFO results, thresholds from Script 17.",
    "21_deployment_decision_engine.py": "Reads validator outputs. No inline params.",
    "22_performance_attribution.py":  "Analytics only. No strategy parameters.",
    "23_risk_analytics.py":           "Analytics only. No strategy parameters.",
    "24_performance_dashboard.py":    "Dashboard. No strategy parameters.",
}

# ═══════════════════════════════════════════════════════════════════════════
# CROSS-PARAMETER CONSISTENCY DECISIONS
# (must be agreed before migrating — documented here for audit trail)
# ═══════════════════════════════════════════════════════════════════════════

CONSISTENCY_DECISIONS = [
    {
        "issue": "adx_weak / ADX_WEAKNESS_EXIT",
        "conflict": "Script 14 = 15.0, Script 16 DEFAULTS = 10",
        "resolution": "Use 10 (Script 16 WFO-validated). adx_weak=10 in strategy_parameters.json. "
                      "Script 14 monitoring uses same value as live exit logic in Scripts 10/16.",
        "rationale": "Exit trigger must be identical between monitoring (Script 14) and "
                     "signal generation (Script 10) and backtest simulation (Script 16). "
                     "Any divergence causes live vs. backtest discrepancy.",
    },
    {
        "issue": "max_positions ceiling",
        "conflict": "Script 09 schedule: max=25 (accounts >=100k). Script 16 WFO result: 40.",
        "resolution": "WFO result wins. Position schedule updated: accounts >=100k → max_positions=40. "
                      "This is the WFO-optimized value (mean=38 across windows).",
        "rationale": "Live execution must match backtest assumptions.",
    },
    {
        "issue": "cb_drawdown semantics",
        "conflict": "Script 11/14 cb_drawdown_halt=-0.15 (halt entries). Script 16 cb_drawdown=-0.30 (halt backtest sim).",
        "resolution": "NOT a conflict — different semantics. Explicitly named separately: "
                      "cb_drawdown_warn=-0.15 (entry halt for live), cb_drawdown_halt=-0.30 "
                      "(full circuit-breaker halt in backtest/live both).",
        "rationale": "The -0.15 threshold in Scripts 11/14 halts NEW ENTRIES only. "
                     "The -0.30 threshold in Script 16 triggers a full trading halt "
                     "requiring recovery before any position is taken.",
    },
    {
        "issue": "momentum SMA period",
        "conflict": "Script 07 MOMENTUM_SMA_PERIOD=200. Architecture sma_slow=250.",
        "resolution": "Change Script 07 to use P.momentum.sma_period=250. "
                      "The formula (Close - SMA_slow)/SMA_slow must use the same period "
                      "as the trend filter to be internally consistent.",
        "rationale": "If trend qualification uses SMA_250 but momentum ranks using SMA_200, "
                     "instruments can qualify on one average but rank on another. This is a "
                     "logic bug, not a tuning preference.",
    },
    {
        "issue": "SMA periods in Script 10 exit signals",
        "conflict": "Script 10 SMA_FAST=50/SMA_SLOW=200. Script 16 sma_fast=100/sma_slow=250.",
        "resolution": "Change Script 10 to P.indicators.sma_fast/sma_slow. "
                      "Death-cross exit (SMA_fast < SMA_slow) must use the same periods "
                      "as the entry qualification to avoid asymmetric entry/exit signals.",
        "rationale": "If you enter when SMA_100 > SMA_250 but exit when SMA_50 < SMA_200, "
                     "you can hold positions that have violated the exit rule for weeks "
                     "without the exit firing. Critical correctness bug.",
    },
]


# ═══════════════════════════════════════════════════════════════════════════
# CLI SUMMARY
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    high   = [k for k, v in MIGRATION_PLAN.items() if v["priority"] == "HIGH"]
    medium = [k for k, v in MIGRATION_PLAN.items() if v["priority"] == "MEDIUM"]

    print("=" * 72)
    print("MIGRATION SUMMARY — Centralized Configuration")
    print("=" * 72)
    print(f"\n  Scripts requiring changes:   {len(MIGRATION_PLAN)}")
    print(f"    HIGH priority:             {len(high)}")
    print(f"    MEDIUM priority:           {len(medium)}")
    print(f"  Scripts with no changes:     {len(NO_CHANGE_SCRIPTS)}")
    print(f"  Consistency decisions:       {len(CONSISTENCY_DECISIONS)}")

    print("\n\nHIGH PRIORITY (implement first — inconsistencies affect live trading):")
    print("-" * 72)
    for script in high:
        info = MIGRATION_PLAN[script]
        print(f"\n  {script}")
        print(f"  Reason: {info['reason'][:70]}...")
        if info["inconsistencies_fixed"]:
            print("  Fixes:")
            for fix in info["inconsistencies_fixed"]:
                print(f"    • {fix}")

    print("\n\nMEDIUM PRIORITY (implement after HIGH — consistency/maintainability):")
    print("-" * 72)
    for script in medium:
        info = MIGRATION_PLAN[script]
        print(f"\n  {script}")
        print(f"  Reason: {info['reason'][:70]}...")

    print("\n\nCRITICAL CONSISTENCY DECISIONS (must be confirmed before migrating):")
    print("-" * 72)
    for i, dec in enumerate(CONSISTENCY_DECISIONS, 1):
        print(f"\n  [{i}] {dec['issue']}")
        print(f"  Conflict:    {dec['conflict']}")
        print(f"  Resolution:  {dec['resolution'][:70]}...")
        print(f"  Rationale:   {dec['rationale'][:70]}...")

    print("\n\nNO CHANGE REQUIRED:")
    print("-" * 72)
    for script, reason in NO_CHANGE_SCRIPTS.items():
        print(f"  {script:45s}  {reason}")

    print("\n\nRECOMMENDED MIGRATION ORDER:")
    print("-" * 72)
    ordered = [
        "1. Confirm 5 consistency decisions above with architecture sign-off",
        "2. Create config/ directory, commit strategy_parameters.json + params.py",
        "3. Migrate Script 05 (indicators — upstream of all signal scripts)",
        "4. Migrate Script 16 (backtest engine DEFAULTS — Script 17 imports from it)",
        "5. Migrate Script 17 (WFO — depends on Script 16 DEFAULTS being correct)",
        "6. Migrate Scripts 06, 07, 08, 09, 10 (signal pipeline in order)",
        "7. Migrate Scripts 11, 14 (live execution & monitoring)",
        "8. Migrate Script 18 (Monte Carlo — independent, can be last)",
        "9. Run params.py self-test: python config/params.py",
        "10. Run full backtest (Script 16) and compare metrics to pre-migration baseline",
    ]
    for step in ordered:
        print(f"  {step}")
    print()
