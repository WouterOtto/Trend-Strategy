#!/usr/bin/env python3
"""
config/params.py
================
Central parameter loader for the Multi-Asset Trend Following Strategy.

USAGE
-----
    from config.params import P          # typed access to all parameters
    from config.params import load_params  # if you need a fresh reload

    # Access parameters:
    P.indicators.sma_fast            # -> int
    P.stops.init_stop_mult           # -> float
    P.walk_forward.is_months         # -> int
    P.optimization_grid              # -> dict[str, list]

DESIGN
------
  Layer 1 — strategy_parameters.json    human-editable, version-controlled
  Layer 2 — params.py (this file)       type-safe, validated, singleton access

  Validation runs at import time.  If strategy_parameters.json is missing a
  required key, has a wrong type, or violates a domain constraint the module
  raises ConfigurationError immediately — before any script logic executes.
  This prevents silent parameter mismatches across pipeline stages.

ADDING NEW PARAMETERS
---------------------
  1. Add the key + value to strategy_parameters.json with a _comment entry.
  2. Add the field to the appropriate dataclass below.
  3. Add a validation assertion in _validate().
  4. Reference P.<section>.<field> in the consuming script.

Architecture: v3.8 (Mar 2026)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Resolve config file location
# ---------------------------------------------------------------------------

def _find_config() -> Path:
    """
    Search for strategy_parameters.json from this file upward to project root.
    Supports running scripts from any working directory.
    """
    # Prefer explicit env var override (useful for tests / CI)
    env_path = os.environ.get("STRATEGY_PARAMS_PATH")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
        raise ConfigurationError(
            f"STRATEGY_PARAMS_PATH set to '{env_path}' but file does not exist"
        )

    # Walk upward from this file's location
    search_start = Path(__file__).resolve().parent
    for parent in [search_start, *search_start.parents]:
        candidate = parent / "config" / "strategy_parameters.json"
        if candidate.exists():
            return candidate
        # Also check if we're already inside config/
        candidate2 = parent / "strategy_parameters.json"
        if candidate2.exists():
            return candidate2

    raise ConfigurationError(
        "strategy_parameters.json not found. "
        "Expected at <project_root>/config/strategy_parameters.json. "
        "Set STRATEGY_PARAMS_PATH environment variable to override."
    )


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class ConfigurationError(Exception):
    """Raised when strategy_parameters.json is missing, malformed, or invalid."""


# ---------------------------------------------------------------------------
# Typed parameter dataclasses
# One dataclass per JSON top-level section.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IndicatorParams:
    sma_fast:   int    # Fast SMA period (Script 5, 6, 10, 16)
    sma_slow:   int    # Slow SMA period (Script 5, 6, 10, 16)
    atr_period: int    # ATR period for stop-loss sizing
    adx_period: int    # ADX period (Wilder standard = 14)


@dataclass(frozen=True)
class TrendQualificationParams:
    adx_threshold:       float  # Entry gate: ADX must be ABOVE this
    adx_weak:            float  # Exit trigger: ADX must be BELOW this (< adx_threshold)
    adx_weakness_days:   int    # Consecutive days below adx_weak before exit fires
    min_history_days:    int    # Minimum price history for indicator validity
    min_valid_indicators:int    # Minimum rows with all indicators non-NaN
    min_data_points:     int    # Minimum bars for SMA_slow computation


@dataclass(frozen=True)
class MomentumParams:
    sma_period:  int        # Must match indicators.sma_slow
    roc_periods: List[int]  # Supplementary ROC lookbacks (not used for ranking)


@dataclass(frozen=True)
class StopParams:
    init_stop_mult:       float  # Initial_Stop = Entry - (mult × ATR)
    trail_stop_mult:      float  # Trailing_Stop = Close - (mult × ATR)
    trail_activation:     float  # Profit % required to activate trailing stop
    max_stop_distance_pct: float # Hard cap on stop distance (prevents outsized sizing)


@dataclass(frozen=True)
class PositionSizingParams:
    risk_per_trade:         float              # Fraction of equity at risk per position
    pos_floor_pct:          float              # Minimum position size
    pos_ceil_pct:           float              # Maximum position size
    entry_limit_offset:     float              # Limit order placed at close + this %
    limit_cancel_days:      int                # Cancel unfilled limit after N days
    position_count_schedule: List[Tuple[float, int]]  # (equity_gte, max_positions)

    def max_positions_for_equity(self, equity: float) -> int:
        """Return max simultaneous positions for the given account equity."""
        result = self.position_count_schedule[0][1]
        for equity_gte, max_pos in self.position_count_schedule:
            if equity >= equity_gte:
                result = max_pos
        return result


@dataclass(frozen=True)
class PortfolioConstraintParams:
    max_crypto_pct:           float  # Hard cap on total crypto exposure
    max_sector_pct:           float  # Warning threshold per sector
    min_cash_pct:             float  # Minimum unallocated equity
    max_top3_concentration_pct: float
    max_single_position_pct:  float
    max_pairwise_correlation: float
    correlation_lookback_days: int


@dataclass(frozen=True)
class ExecutionParams:
    slippage_stock:  float  # Fill slippage for equities
    slippage_crypto: float  # Fill slippage for crypto
    cost_bps:        int    # Round-trip transaction cost in basis points


@dataclass(frozen=True)
class CircuitBreakerParams:
    cb_drawdown_halt:       float  # Portfolio DD below this → halt all entries
    cb_drawdown_warn:       float  # → HIGH alert in monitoring
    cb_drawdown_watch:      float  # → MEDIUM alert in monitoring
    cb_recovery_pct:        float  # Equity must recover this % before resuming
    cb_min_halt_days:       int    # Minimum days before re-entry after halt
    cb_vix_enter:           float  # VIX above this → halt new entries
    cb_vix_resume:          float  # VIX must drop below this to resume
    cb_vix_resume_days:     int    # Consecutive days VIX < cb_vix_resume to resume
    cb_data_staleness_days: int    # Market data older than this → CRITICAL
    stop_proximity_high_pct:   float  # Distance-to-stop below this → HIGH alert
    stop_proximity_medium_pct: float  # Distance-to-stop below this → MEDIUM alert


@dataclass(frozen=True)
class BacktestingParams:
    initial_equity:          float
    risk_free_rate:          float
    trading_days_per_year:   int
    benchmark_symbol:        str


@dataclass(frozen=True)
class WalkForwardParams:
    is_months:             int    # In-sample optimization window
    oos_months:            int    # Out-of-sample validation window
    roll_months:           int    # Step size between windows
    stability_excellent:   float
    stability_good:        float
    stability_acceptable:  float
    oos_consistency_pass:  float
    oos_consistency_warn:  float
    param_cv_excellent:    float
    param_cv_good:         float


@dataclass(frozen=True)
class MonteCarloGates:
    mc1_median_cagr_min:    float
    mc2_p5_equity_pct_min:  float
    mc3_p95_maxdd_max:      float
    mc4_median_sharpe_min:  float
    mc5_ruin_prob_max:      float
    mc6_m3_ruin_prob_max:   float


@dataclass(frozen=True)
class MonteCarloParams:
    n_paths:                int
    n_paths_fast:           int
    block_length:           int
    risk_free_rate:         float
    trading_days_per_year:  int
    soft_ruin_level:        float
    hard_dd_limit:          float
    seed:                   int
    report_percentiles:     List[int]
    gates:                  MonteCarloGates


# ---------------------------------------------------------------------------
# Root container
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StrategyParams:
    """
    Single access point for all strategy parameters.

    Scripts import and use:
        from config.params import P
        P.indicators.sma_fast
        P.stops.init_stop_mult
        P.walk_forward.is_months
        P.optimization_grid              # dict[str, list]
        P.optimization_grid_fast         # dict[str, list]
        P.fixed_params                   # dict[str, Any]
        P.as_backtest_defaults()         # dict matching Script 16 DEFAULTS format
    """
    indicators:            IndicatorParams
    trend_qualification:   TrendQualificationParams
    momentum:              MomentumParams
    stops:                 StopParams
    position_sizing:       PositionSizingParams
    portfolio_constraints: PortfolioConstraintParams
    execution:             ExecutionParams
    circuit_breakers:      CircuitBreakerParams
    backtesting:           BacktestingParams
    walk_forward:          WalkForwardParams
    monte_carlo:           MonteCarloParams
    optimization_grid:     Dict[str, List[Any]]
    optimization_grid_fast: Dict[str, List[Any]]
    fixed_params:          Dict[str, Any]

    def as_backtest_defaults(self) -> Dict[str, Any]:
        """
        Return a flat dict matching Script 16's DEFAULTS format.
        Keeps Script 17's `from backtest_engine_16 import DEFAULTS` working
        while all values now originate from strategy_parameters.json.
        """
        return dict(
            sma_fast            = self.indicators.sma_fast,
            sma_slow            = self.indicators.sma_slow,
            adx_threshold       = self.trend_qualification.adx_threshold,
            adx_weak            = self.trend_qualification.adx_weak,
            adx_weakness_days   = self.trend_qualification.adx_weakness_days,
            momentum_period     = self.momentum.sma_period,
            init_stop_mult      = self.stops.init_stop_mult,
            trail_stop_mult     = self.stops.trail_stop_mult,
            trail_activation    = self.stops.trail_activation,
            max_positions       = self.position_sizing.max_positions_for_equity(
                                    self.backtesting.initial_equity),
            risk_per_trade      = self.position_sizing.risk_per_trade,
            pos_floor_pct       = self.position_sizing.pos_floor_pct,
            pos_ceil_pct        = self.position_sizing.pos_ceil_pct,
            limit_slip          = self.position_sizing.entry_limit_offset,
            limit_cancel_days   = self.position_sizing.limit_cancel_days,
            slippage_stock      = self.execution.slippage_stock,
            slippage_crypto     = self.execution.slippage_crypto,
            cost_bps            = self.execution.cost_bps,
            initial_equity      = self.backtesting.initial_equity,
            cb_drawdown         = self.circuit_breakers.cb_drawdown_halt,
            cb_recovery_pct     = self.circuit_breakers.cb_recovery_pct,
            cb_min_halt_days    = self.circuit_breakers.cb_min_halt_days,
            cb_vix_enter        = self.circuit_breakers.cb_vix_enter,
            cb_vix_resume       = self.circuit_breakers.cb_vix_resume,
            cb_vix_resume_days  = self.circuit_breakers.cb_vix_resume_days,
        )


# ---------------------------------------------------------------------------
# Parser & validator
# ---------------------------------------------------------------------------

def _require(d: dict, *keys: str, context: str = "") -> Any:
    """
    Walk a nested dict by dotted key path and return the leaf value.
    Raises ConfigurationError with clear message if any key is missing.
    """
    node = d
    path = []
    for key in keys:
        path.append(key)
        if not isinstance(node, dict) or key not in node:
            loc = ".".join(path)
            raise ConfigurationError(
                f"Missing required key '{loc}' in strategy_parameters.json"
                + (f" [{context}]" if context else "")
            )
        node = node[key]
    return node


def _validate(p: StrategyParams) -> None:
    """
    Domain-level validation: types, ranges, and cross-parameter consistency.
    Raises ConfigurationError on the first violation found.
    """
    errors: List[str] = []

    def chk(condition: bool, msg: str) -> None:
        if not condition:
            errors.append(msg)

    i = p.indicators
    chk(0 < i.sma_fast < i.sma_slow,
        f"sma_fast ({i.sma_fast}) must be < sma_slow ({i.sma_slow})")
    chk(10 <= i.atr_period <= 50, f"atr_period out of range [10,50]: {i.atr_period}")
    chk(i.adx_period == 14, f"adx_period should be 14 (Wilder standard), got {i.adx_period}")

    t = p.trend_qualification
    chk(t.adx_weak < t.adx_threshold,
        f"adx_weak ({t.adx_weak}) must be < adx_threshold ({t.adx_threshold})")
    chk(t.adx_weakness_days >= 1, f"adx_weakness_days must be >= 1")
    chk(t.min_history_days >= 200, f"min_history_days must be >= 200 (SMA_slow warmup)")

    m = p.momentum
    chk(m.sma_period == i.sma_slow,
        f"momentum.sma_period ({m.sma_period}) must equal indicators.sma_slow ({i.sma_slow})")

    s = p.stops
    chk(0 < s.init_stop_mult <= 5.0,
        f"init_stop_mult out of range (0, 5.0]: {s.init_stop_mult}")
    chk(0 < s.trail_stop_mult <= 6.0,
        f"trail_stop_mult out of range (0, 6.0]: {s.trail_stop_mult}")
    chk(0 < s.trail_activation < 1.0,
        f"trail_activation must be in (0, 1): {s.trail_activation}")

    ps = p.position_sizing
    chk(0 < ps.risk_per_trade <= 0.05,
        f"risk_per_trade > 5% is dangerous: {ps.risk_per_trade}")
    chk(ps.pos_floor_pct < ps.pos_ceil_pct,
        f"pos_floor_pct ({ps.pos_floor_pct}) must be < pos_ceil_pct ({ps.pos_ceil_pct})")

    pc = p.portfolio_constraints
    chk(0 < pc.max_crypto_pct <= 0.30,
        f"max_crypto_pct should not exceed 30%: {pc.max_crypto_pct}")
    chk(0 < pc.max_pairwise_correlation <= 1.0,
        f"max_pairwise_correlation out of [0,1]: {pc.max_pairwise_correlation}")

    cb = p.circuit_breakers
    chk(cb.cb_drawdown_halt < cb.cb_drawdown_warn < cb.cb_drawdown_watch < 0,
        f"Drawdown thresholds must be negative and ordered: "
        f"halt={cb.cb_drawdown_halt}, warn={cb.cb_drawdown_warn}, watch={cb.cb_drawdown_watch}")
    chk(cb.cb_vix_resume < cb.cb_vix_enter,
        f"cb_vix_resume ({cb.cb_vix_resume}) must be < cb_vix_enter ({cb.cb_vix_enter})")

    bt = p.backtesting
    chk(bt.initial_equity > 0, f"initial_equity must be positive: {bt.initial_equity}")
    chk(0 <= bt.risk_free_rate < 0.20,
        f"risk_free_rate out of [0, 0.20): {bt.risk_free_rate}")

    wf = p.walk_forward
    chk(wf.is_months >= 12, f"is_months should be >= 12 for meaningful optimization")
    chk(wf.oos_months >= 6, f"oos_months should be >= 6 to cover a full regime")
    chk(wf.stability_acceptable < wf.stability_good < wf.stability_excellent,
        "Walk-forward stability thresholds must be ordered: acceptable < good < excellent")

    og = p.optimization_grid
    for param, values in og.items():
        chk(len(values) >= 2,
            f"optimization_grid.{param} must have >= 2 values to be meaningful")

    mc = p.monte_carlo
    chk(mc.n_paths >= 1000, f"n_paths < 1000 gives unreliable CI estimates")
    chk(0 < mc.soft_ruin_level < 1.0, f"soft_ruin_level out of (0, 1)")

    if errors:
        bullet_list = "\n  - ".join(errors)
        raise ConfigurationError(
            f"strategy_parameters.json has {len(errors)} validation error(s):\n"
            f"  - {bullet_list}"
        )


def _parse(raw: Dict[str, Any]) -> StrategyParams:
    """Convert raw JSON dict into typed StrategyParams, failing fast on errors."""

    def _r(*keys):
        return _require(raw, *keys)

    # ── indicators ───────────────────────────────────────────────────────────
    ind_raw = _r("indicators")
    indicators = IndicatorParams(
        sma_fast   = int(ind_raw["sma_fast"]),
        sma_slow   = int(ind_raw["sma_slow"]),
        atr_period = int(ind_raw["atr_period"]),
        adx_period = int(ind_raw["adx_period"]),
    )

    # ── trend_qualification ──────────────────────────────────────────────────
    tq_raw = _r("trend_qualification")
    trend_qualification = TrendQualificationParams(
        adx_threshold        = float(tq_raw["adx_threshold"]),
        adx_weak             = float(tq_raw["adx_weak"]),
        adx_weakness_days    = int(tq_raw["adx_weakness_days"]),
        min_history_days     = int(tq_raw["min_history_days"]),
        min_valid_indicators = int(tq_raw["min_valid_indicators"]),
        min_data_points      = int(tq_raw["min_data_points"]),
    )

    # ── momentum ─────────────────────────────────────────────────────────────
    mom_raw = _r("momentum")
    momentum = MomentumParams(
        sma_period  = int(mom_raw["sma_period"]),
        roc_periods = [int(x) for x in mom_raw["roc_periods"]],
    )

    # ── stops ────────────────────────────────────────────────────────────────
    st_raw = _r("stops")
    stops = StopParams(
        init_stop_mult        = float(st_raw["init_stop_mult"]),
        trail_stop_mult       = float(st_raw["trail_stop_mult"]),
        trail_activation      = float(st_raw["trail_activation"]),
        max_stop_distance_pct = float(st_raw["max_stop_distance_pct"]),
    )

    # ── position_sizing ──────────────────────────────────────────────────────
    ps_raw = _r("position_sizing")
    schedule_raw = ps_raw["position_count_schedule"]
    schedule = [
        (float(entry["equity_gte"]), int(entry["max_positions"]))
        for entry in schedule_raw
    ]
    schedule.sort(key=lambda x: x[0])  # ensure ascending order
    position_sizing = PositionSizingParams(
        risk_per_trade          = float(ps_raw["risk_per_trade"]),
        pos_floor_pct           = float(ps_raw["pos_floor_pct"]),
        pos_ceil_pct            = float(ps_raw["pos_ceil_pct"]),
        entry_limit_offset      = float(ps_raw["entry_limit_offset"]),
        limit_cancel_days       = int(ps_raw["limit_cancel_days"]),
        position_count_schedule = schedule,
    )

    # ── portfolio_constraints ────────────────────────────────────────────────
    pc_raw = _r("portfolio_constraints")
    portfolio_constraints = PortfolioConstraintParams(
        max_crypto_pct              = float(pc_raw["max_crypto_pct"]),
        max_sector_pct              = float(pc_raw["max_sector_pct"]),
        min_cash_pct                = float(pc_raw["min_cash_pct"]),
        max_top3_concentration_pct  = float(pc_raw["max_top3_concentration_pct"]),
        max_single_position_pct     = float(pc_raw["max_single_position_pct"]),
        max_pairwise_correlation    = float(pc_raw["max_pairwise_correlation"]),
        correlation_lookback_days   = int(pc_raw["correlation_lookback_days"]),
    )

    # ── execution ────────────────────────────────────────────────────────────
    ex_raw = _r("execution")
    execution = ExecutionParams(
        slippage_stock  = float(ex_raw["slippage_stock"]),
        slippage_crypto = float(ex_raw["slippage_crypto"]),
        cost_bps        = int(ex_raw["cost_bps"]),
    )

    # ── circuit_breakers ─────────────────────────────────────────────────────
    cb_raw = _r("circuit_breakers")
    circuit_breakers = CircuitBreakerParams(
        cb_drawdown_halt          = float(cb_raw["cb_drawdown_halt"]),
        cb_drawdown_warn          = float(cb_raw["cb_drawdown_warn"]),
        cb_drawdown_watch         = float(cb_raw["cb_drawdown_watch"]),
        cb_recovery_pct           = float(cb_raw["cb_recovery_pct"]),
        cb_min_halt_days          = int(cb_raw["cb_min_halt_days"]),
        cb_vix_enter              = float(cb_raw["cb_vix_enter"]),
        cb_vix_resume             = float(cb_raw["cb_vix_resume"]),
        cb_vix_resume_days        = int(cb_raw["cb_vix_resume_days"]),
        cb_data_staleness_days    = int(cb_raw["cb_data_staleness_days"]),
        stop_proximity_high_pct   = float(cb_raw["stop_proximity_high_pct"]),
        stop_proximity_medium_pct = float(cb_raw["stop_proximity_medium_pct"]),
    )

    # ── backtesting ──────────────────────────────────────────────────────────
    bt_raw = _r("backtesting")
    backtesting = BacktestingParams(
        initial_equity        = float(bt_raw["initial_equity"]),
        risk_free_rate        = float(bt_raw["risk_free_rate"]),
        trading_days_per_year = int(bt_raw["trading_days_per_year"]),
        benchmark_symbol      = str(bt_raw["benchmark_symbol"]),
    )

    # ── walk_forward ─────────────────────────────────────────────────────────
    wf_raw = _r("walk_forward")
    walk_forward = WalkForwardParams(
        is_months            = int(wf_raw["is_months"]),
        oos_months           = int(wf_raw["oos_months"]),
        roll_months          = int(wf_raw["roll_months"]),
        stability_excellent  = float(wf_raw["stability_excellent"]),
        stability_good       = float(wf_raw["stability_good"]),
        stability_acceptable = float(wf_raw["stability_acceptable"]),
        oos_consistency_pass = float(wf_raw["oos_consistency_pass"]),
        oos_consistency_warn = float(wf_raw["oos_consistency_warn"]),
        param_cv_excellent   = float(wf_raw["param_cv_excellent"]),
        param_cv_good        = float(wf_raw["param_cv_good"]),
    )

    # ── monte_carlo ──────────────────────────────────────────────────────────
    mc_raw   = _r("monte_carlo")
    g_raw    = _require(mc_raw, "gates")
    mc_gates = MonteCarloGates(
        mc1_median_cagr_min    = float(g_raw["mc1_median_cagr_min"]),
        mc2_p5_equity_pct_min  = float(g_raw["mc2_p5_equity_pct_min"]),
        mc3_p95_maxdd_max      = float(g_raw["mc3_p95_maxdd_max"]),
        mc4_median_sharpe_min  = float(g_raw["mc4_median_sharpe_min"]),
        mc5_ruin_prob_max      = float(g_raw["mc5_ruin_prob_max"]),
        mc6_m3_ruin_prob_max   = float(g_raw["mc6_m3_ruin_prob_max"]),
    )
    monte_carlo = MonteCarloParams(
        n_paths               = int(mc_raw["n_paths"]),
        n_paths_fast          = int(mc_raw["n_paths_fast"]),
        block_length          = int(mc_raw["block_length"]),
        risk_free_rate        = float(mc_raw["risk_free_rate"]),
        trading_days_per_year = int(mc_raw["trading_days_per_year"]),
        soft_ruin_level       = float(mc_raw["soft_ruin_level"]),
        hard_dd_limit         = float(mc_raw["hard_dd_limit"]),
        seed                  = int(mc_raw["seed"]),
        report_percentiles    = [int(x) for x in mc_raw["report_percentiles"]],
        gates                 = mc_gates,
    )

    # ── optimization grids & fixed params ────────────────────────────────────
    raw_grid      = _r("optimization_grid")
    raw_grid_fast = _r("optimization_grid_fast")
    raw_fixed     = _r("fixed_params")

    # Strip _comment keys (JSON workaround for comments)
    optimization_grid      = {k: v for k, v in raw_grid.items()      if not k.startswith("_")}
    optimization_grid_fast = {k: v for k, v in raw_grid_fast.items() if not k.startswith("_")}
    fixed_params           = {k: v for k, v in raw_fixed.items()      if not k.startswith("_")}

    return StrategyParams(
        indicators             = indicators,
        trend_qualification    = trend_qualification,
        momentum               = momentum,
        stops                  = stops,
        position_sizing        = position_sizing,
        portfolio_constraints  = portfolio_constraints,
        execution              = execution,
        circuit_breakers       = circuit_breakers,
        backtesting            = backtesting,
        walk_forward           = walk_forward,
        monte_carlo            = monte_carlo,
        optimization_grid      = optimization_grid,
        optimization_grid_fast = optimization_grid_fast,
        fixed_params           = fixed_params,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_params(config_path: Optional[Path] = None) -> StrategyParams:
    """
    Load, parse, and validate strategy_parameters.json.

    Args:
        config_path: Explicit path to JSON file. If None, auto-discovered.

    Returns:
        Validated StrategyParams instance.

    Raises:
        ConfigurationError: If file is missing, malformed, or fails validation.
    """
    path = config_path or _find_config()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise ConfigurationError(f"strategy_parameters.json not found at: {path}")
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"strategy_parameters.json is not valid JSON: {exc}"
        )

    params = _parse(raw)
    _validate(params)
    return params


# ---------------------------------------------------------------------------
# Singleton — loaded once at import, available as P
# ---------------------------------------------------------------------------

try:
    P: StrategyParams = load_params()
except ConfigurationError as _cfg_err:
    import warnings
    warnings.warn(
        f"[config.params] Could not load strategy_parameters.json: {_cfg_err}\n"
        "P is not available. Call load_params(path) explicitly.",
        stacklevel=2,
    )
    P = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# CLI self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    print("=" * 72)
    print("Strategy Parameter Loader — Self Test")
    print("=" * 72)

    try:
        params = load_params()
    except ConfigurationError as e:
        print(f"\n[FAIL] {e}")
        sys.exit(1)

    print(f"\n[OK] strategy_parameters.json loaded and validated successfully\n")

    sections = [
        ("indicators",            params.indicators),
        ("trend_qualification",   params.trend_qualification),
        ("momentum",              params.momentum),
        ("stops",                 params.stops),
        ("position_sizing",       params.position_sizing),
        ("portfolio_constraints", params.portfolio_constraints),
        ("execution",             params.execution),
        ("circuit_breakers",      params.circuit_breakers),
        ("backtesting",           params.backtesting),
        ("walk_forward",          params.walk_forward),
    ]

    for name, section in sections:
        print(f"  [{name}]")
        for attr, val in section.__dict__.items():
            print(f"    {attr:35s} = {val}")
        print()

    print(f"  [optimization_grid]  {len(params.optimization_grid)} parameters")
    for k, v in params.optimization_grid.items():
        print(f"    {k:30s} = {v}")

    print(f"\n  [fixed_params]  {len(params.fixed_params)} parameters")
    for k, v in params.fixed_params.items():
        print(f"    {k:30s} = {v}")

    print(f"\n  [monte_carlo gates]")
    for attr, val in params.monte_carlo.gates.__dict__.items():
        print(f"    {attr:35s} = {val}")

    combo_count = 1
    for v in params.optimization_grid.values():
        combo_count *= len(v)
    print(f"\n  WFO grid combinations: {combo_count:,}")
    print(f"  as_backtest_defaults() keys: {len(params.as_backtest_defaults())}")

    print(f"\n[OK] All checks passed.")
