#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
00_run_pipeline.py
==================
Master Pipeline Orchestrator — Architecture v3.2 (Feb 2026)

Runs all strategy scripts incrementally in the correct dependency order.
Each step is independently logged; failures are isolated and reported.

PIPELINE MODES
--------------
  setup       First-time initialization (full download + validation, ~2-3 hrs)
  daily       Portfolio monitoring (consolidate portfolio symbols only, ~1 min)
  weekly      Saturday run: consolidate all + refresh stops/exits (~2-8 hrs, run overnight)
  monthly     End-of-month rebalancing (Saturday, ~2-12 hrs, run overnight) + analytics
  quarterly   Monthly rebalancing + data quality audit + analytics (~3-4 hrs)
  backtest    Full backtest validation pipeline (engine + optimizer + monte carlo + validators, hours/days)
  validation  Validate existing backtest results only (validators + decision engine, ~5 min)
  analytics   Performance review only (attribution + risk + dashboard, ~30 sec)
  report      Re-generate PDF report from latest rebalancing JSON
  custom      Supply --steps explicitly for granular control

QUICK-START
-----------
  # Daily monitoring (consolidates portfolio symbols only, very fast):
  python scripts/00_trend_strategy_pipeline.py daily --account-equity 50000

  # Weekly run (every Saturday - consolidate all + refresh stops/exits):
  python scripts/00_trend_strategy_pipeline.py weekly \
      --as-of-date 2026-03-01 \
      --account-equity 50000

  # Monthly rebalancing (last Saturday of month):
  python scripts/00_trend_strategy_pipeline.py monthly \
      --as-of-date 2026-02-28 \
      --account-equity 50000 \
      --vix 18.5

  # Quarterly review (monthly rebalancing + full data quality audit):
  python scripts/00_trend_strategy_pipeline.py quarterly \
      --as-of-date 2026-03-31 \
      --account-equity 50000 \
      --vix 16.8

  # Performance analytics only (fast review of returns, risk, attribution):
  python scripts/00_trend_strategy_pipeline.py analytics \
      --month 2026-02 \
      --account-equity 50000 \
      --benchmark SPY.US

  # Full backtest validation pipeline (long-running, hours/days):
  python scripts/00_trend_strategy_pipeline.py backtest \
      --backtest-start 2019-01-01 \
      --backtest-end 2024-12-31 \
      --initial-equity 10000

  # Validate existing backtest results (fast, ~5 min):
  python scripts/00_trend_strategy_pipeline.py validation

  # Re-generate PDF report only:
  python scripts/00_trend_strategy_pipeline.py report --month 2026-01

  # Dry-run any mode (no files written):
  python scripts/00_trend_strategy_pipeline.py monthly \
      --as-of-date 2026-01-31 --account-equity 50000 --dry-run

  # Run specific steps only:
  python scripts/00_trend_strategy_pipeline.py custom \
      --steps 4,5,6 --as-of-date 2026-01-31

  # Custom with mode inheritance (monthly behavior, custom steps):
  python scripts/00_trend_strategy_pipeline.py custom --as-monthly \
      --steps 1,3,4,5,6,7,8,11,12 \
      --as-of-date 2026-01-31 --account-equity 50000

DEPENDENCIES
------------
  All scripts must live in the same directory as this orchestrator, or in a
  subdirectory named 'scripts/'.  The orchestrator searches both locations.

EXIT CODES
----------
  0  All steps succeeded
  1  One or more steps failed (see summary table)
  2  Argument / configuration error
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

VERSION = "3.2.0"
SCRIPT_DIR = Path(__file__).resolve().parent

# Maps canonical script numbers → file names
SCRIPT_MAP: dict[int, str] = {
    #1:  "01_download_eodhd_bulk.py",
    #2:  "02_download_yahoo_fundamentals.py",
    #3:  "03_consolidate_validate_data.py",
    4:  "04_screen_universe.py",
    5:  "05_calculate_indicators.py",
    6:  "06_qualify_trends.py",
    7:  "07_rank_momentum.py",
    8:  "08_calculate_stops.py",
    9:  "09_calculate_position_sizes.py",
    10: "10_generate_exit_signals.py",
    11: "11_monthly_rebalancing.py",
    12: "12_generate_recommendation_report.py",
    14: "14_daily_monitoring.py",
    15: "15_generate_technical_charts.py",
    16: "16_backtest_engine.py",
    17: "17_walk_forward_optimizer.py",
    18: "18_monte_carlo_simulator.py",
    19: "19_backtest_validator.py",
    20: "20_oos_validator.py",
    21: "21_deployment_decision_engine.py",
    22: "22_performance_attribution.py",
    23: "23_risk_analytics.py",
    24: "24_performance_dashboard.py",
}

SCRIPT_LABELS: dict[int, str] = {
    #1:  "Download EODHD bulk data",
    #2:  "Download Yahoo fundamentals",
    #3:  "Consolidate & validate data",
    4:  "Screen universe",
    5:  "Calculate indicators",
    6:  "Qualify trends",
    7:  "Rank momentum",
    8:  "Calculate stop-losses",
    9:  "Calculate position sizes",
    10: "Generate exit signals",
    11: "Monthly rebalancing",
    12: "Generate recommendation report (PDF)",
    14: "Daily portfolio monitoring",
    15: "Generate technical analysis charts",
    16: "Backtest engine",
    17: "Walk-forward optimizer",
    18: "Monte Carlo simulator",
    19: "Backtest validator",
    20: "Out-of-sample validator",
    21: "Deployment decision engine",
    22: "Performance attribution",
    23: "Risk analytics",
    24: "Performance dashboard",
}

# Pipeline step sequences per mode
PIPELINES: dict[str, List[int]] = {
    "setup":      [4, 5, 6, 7],           # One-time: full download + validate
    "daily":      [14],                      # Daily: download + consolidate portfolio + monitor
    "weekly":     [9, 10, 14],               # Weekly: download + consolidate all + stops + exits + monitor
    "monthly":    [4, 5, 6, 7, 8, 9, 10, 11, 12, 15, 22, 23, 24],  # Monthly: full rebalancing + analytics
    "quarterly":  [4, 5, 6, 7, 8, 9, 10, 11, 12, 15, 22, 23, 24],  # Quarterly: + data validation + analytics
    "backtest":   [16, 17, 18, 19, 20, 21],        # Backtest: full validation pipeline (long-running)
    "validation": [19, 20, 21],                    # Validation: validate existing backtest results (fast)
    "analytics":  [22, 23, 24],                    # Analytics: performance review only (fast)
    "report":     [12],                            # Report-only: regenerate PDF
}

# ANSI colours (disabled on Windows or when not a tty)
_USE_COLOUR = sys.stdout.isatty() and os.name != "nt"

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOUR else text

CLR_GREEN  = "32;1"
CLR_RED    = "31;1"
CLR_YELLOW = "33;1"
CLR_CYAN   = "36;1"
CLR_BOLD   = "1"
CLR_DIM    = "2"

# ─────────────────────────────────────────────────────────────────────────────
# DEPENDENCY MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

# Packages that must be present for the pipeline to run.
# Format: { pip_package_name: import_name }
REQUIRED_PACKAGES: dict[str, str] = {
    "yfinance": "yfinance",
    "tqdm":     "tqdm",
    "pandas":   "pandas",
    "numpy":    "numpy",
    "pyarrow":  "pyarrow",
}

# Per-script timeout overrides (seconds).
# Script 02 fetches Yahoo fundamentals for thousands of symbols — it legitimately
# needs 60-120 min on a full run.  The global --timeout applies to all other scripts.
SCRIPT_TIMEOUTS: dict[int, int] = {
    # 3:  3600,  # 1 hour (default)  — Consolidate-only
    # Note: Script 03 timeout is dynamically adjusted based on mode:
    #   - daily mode:      600s (10 min)    — portfolio symbols only (~10-50 symbols)
    #   - weekly mode:     43200s (12 hours) — all symbols, incremental consolidation
    #   - monthly mode:    43200s (12 hours) — all symbols, incremental consolidation  
    #   - quarterly mode:  14400s (4 hours)  — all symbols, incremental + full validation
}


def check_and_install_deps(logger: logging.Logger, auto_install: bool = True) -> List[str]:
    """
    Verify all required Python packages are importable.
    If auto_install=True, attempt pip install for any that are missing.
    Returns a list of packages that are still missing after the attempt.
    """
    import importlib
    still_missing: List[str] = []

    for pip_name, import_name in REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(import_name)
        except ImportError:
            logger.warning(f"  Missing package: {pip_name}")
            if auto_install:
                logger.info(f"  → Installing {pip_name} ...")
                print(f"  {_c(CLR_YELLOW, f'Installing missing package: {pip_name} ...')}")
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--quiet",
                     "--break-system-packages", pip_name],
                    capture_output=True, text=True,
                )
                if result.returncode == 0:
                    logger.info(f"  ✔ {pip_name} installed successfully.")
                    print(f"  {_c(CLR_GREEN, f'✔ {pip_name} installed.')}")
                else:
                    logger.error(
                        f"  pip install {pip_name} failed:\n{result.stderr.strip()}"
                    )
                    still_missing.append(pip_name)
            else:
                still_missing.append(pip_name)

    return still_missing


# ─────────────────────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StepResult:
    step_num:    int
    label:       str
    status:      str          # "OK" | "FAIL" | "SKIP" | "WARN"
    returncode:  int  = 0
    duration_s:  float = 0.0
    cmd:         str  = ""
    stdout_tail: str  = ""
    stderr_tail: str  = ""

    @property
    def ok(self) -> bool:
        return self.status in ("OK", "WARN", "SKIP")

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING SETUP
# ─────────────────────────────────────────────────────────────────────────────

def setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"pipeline_{ts}.log"

    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level=logging.DEBUG,
        format=fmt,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logger = logging.getLogger("pipeline")
    # Keep console at INFO; file gets DEBUG
    logging.getLogger().handlers[1].setLevel(logging.INFO)
    logger.info(f"Pipeline log: {log_file}")
    return logger

# ─────────────────────────────────────────────────────────────────────────────
# SCRIPT DISCOVERY
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_cache_path(cache_path: Optional[Path]) -> Optional[Path]:
    """
    Resolve a relative cache path against both SCRIPT_DIR and its parent
    (project root).  Returns the first existing match, or None.

    Handles two common layouts:
        Layout A: 00_trend_strategy_pipeline.py at project root
                  → data_cache/ is at SCRIPT_DIR / cache_path
        Layout B: 00_trend_strategy_pipeline.py inside scripts/
                  → data_cache/ is at SCRIPT_DIR.parent / cache_path
    """
    if cache_path is None:
        return None
    if cache_path.is_absolute():
        return cache_path if cache_path.exists() else None
    candidates = [
        SCRIPT_DIR          / cache_path,   # Layout A
        SCRIPT_DIR.parent   / cache_path,   # Layout B (scripts/ subfolder)
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def find_script(num: int) -> Optional[Path]:
    """
    Locate a script file by number.
    Searches: same directory as orchestrator, then ./scripts/ sub-directory.
    """
    name = SCRIPT_MAP.get(num)
    if name is None:
        return None
    candidates = [
        SCRIPT_DIR / name,
        SCRIPT_DIR / "scripts" / name,
    ]
    for p in candidates:
        if p.exists():
            return p
    return None

# ─────────────────────────────────────────────────────────────────────────────
# ARGUMENT BUILDER  (maps parsed args → per-script CLI flags)
# ─────────────────────────────────────────────────────────────────────────────

def build_args(num: int, ns: argparse.Namespace) -> List[str]:
    """
    Return the list of CLI flags for script `num` given the master
    namespace `ns`.  Only flags relevant to each script are included.
    """
    a: List[str] = []

    # ── Scripts 4-7, 9-10: as-of-date ────────────────────────────────────────
    if num in (4, 5, 6, 7, 9, 10):
        if ns.as_of_date:
            a += ["--as-of-date", ns.as_of_date]
        if num == 9 and getattr(ns, "force_friday", False):
            a += ["--force-friday"]
        if num == 10 and getattr(ns, "check_rotation", True):
            # --check-rotation requires --max-positions.
            # Resolve it in priority order:
            #   1. Explicit CLI --max-positions
            #   2. Script 08 output (data_cache/portfolio/position_sizes.json)
            max_pos = ns.max_positions
            if not max_pos:
                import json as _json
                for base in (SCRIPT_DIR, SCRIPT_DIR.parent):
                    sizes_file = base / "data_cache" / "portfolio" / "position_sizes.json"
                    if sizes_file.exists():
                        try:
                            with open(sizes_file) as _f:
                                max_pos = _json.load(_f).get("max_positions")
                        except Exception:
                            pass
                        break
            if max_pos:
                a += ["--check-rotation", "--max-positions", str(max_pos)]
            # If max_pos still unknown, omit --check-rotation entirely so
            # Script 10 runs without the rotation check rather than erroring.

    # ── Script 8: stop-loss calculator ───────────────────────────────────────
    if num == 8:
        if ns.as_of_date:
            a += ["--as-of-date", ns.as_of_date]
        if ns.account_equity:
            a += ["--account-equity", str(ns.account_equity)]
        if ns.max_positions:
            a += ["--max-positions", str(ns.max_positions)]

    # ── Script 9: position sizes ──────────────────────────────────────────────
    if num == 9:
        if ns.account_equity:
            a += ["--account-equity", str(ns.account_equity)]
        if ns.max_positions:
            a += ["--max-positions", str(ns.max_positions)]

    # ── Script 11: monthly rebalancing ────────────────────────────────────────
    if num == 11:
        if ns.account_equity:
            a += ["--account-equity", str(ns.account_equity)]
        if ns.as_of_date:
            a += ["--rebalance-date", ns.as_of_date]
        if ns.vix is not None:
            a += ["--vix", str(ns.vix)]
        override = getattr(ns, "override_circuit_breaker", None)
        if override:
            a += ["--override-circuit-breaker", override]

    # ── Script 12: recommendation report ─────────────────────────────────────
    if num == 12:
        month = getattr(ns, "month", None)
        if month:
            a += ["--month", month]
        elif ns.as_of_date:
            # Derive YYYY-MM from as-of-date
            a += ["--month", ns.as_of_date[:7]]

    # ── Script 14: daily monitoring ───────────────────────────────────────────
    if num == 14:
        if ns.as_of_date:
            a += ["--as-of-date", ns.as_of_date]
        if ns.account_equity:
            a += ["--account-equity", str(ns.account_equity)]
        if ns.vix is not None:
            a += ["--vix", str(ns.vix)]

    # ── Script 15: technical analysis charts ──────────────────────────────────
    if num == 15:
        chart_symbols = getattr(ns, "chart_symbols", None)
        if chart_symbols:
            a += ["--symbols", chart_symbols]
        else:
            # Default: chart all qualified trends (consistent with monthly run)
            a += ["--filter", "all"]
        chart_lookback = getattr(ns, "chart_lookback", None)
        if chart_lookback:
            a += ["--lookback", str(chart_lookback)]

    # ── Scripts 22-24: Performance analytics ──────────────────────────────────
    if num == 22:
        # Script 22: Performance attribution (uses --month)
        month_param = getattr(ns, "month", None)
        as_of_date = getattr(ns, "as_of_date", None)
        
        if month_param:
            a += ["--month", month_param]
        elif as_of_date:
            # Derive --month from as-of-date (YYYY-MM-DD → YYYY-MM)
            try:
                month_str = as_of_date[:7]  # e.g., "2026-02-28" → "2026-02"
                a += ["--month", month_str]
            except:
                pass  # Script will use default
        
        # Forward account equity
        if hasattr(ns, "account_equity") and ns.account_equity is not None:
            a += ["--account-equity", str(ns.account_equity)]
        
        # Forward benchmark
        benchmark = getattr(ns, "benchmark", None)
        if benchmark:
            a += ["--benchmark", benchmark]
    
    elif num == 23:
        # Script 23: Risk analytics (uses --as-of, not --month)
        as_of_date = getattr(ns, "as_of_date", None)
        month_param = getattr(ns, "month", None)
        
        if as_of_date:
            # Use as-of-date directly (it's already YYYY-MM-DD)
            a += ["--as-of", as_of_date]
        elif month_param:
            # Derive as-of date from month (use last day of month)
            try:
                import calendar
                year, month = map(int, month_param.split('-'))
                last_day = calendar.monthrange(year, month)[1]
                as_of_str = f"{year:04d}-{month:02d}-{last_day:02d}"
                a += ["--as-of", as_of_str]
            except:
                pass  # Script will use default (today)
        
        # Forward account equity
        if hasattr(ns, "account_equity") and ns.account_equity is not None:
            a += ["--account-equity", str(ns.account_equity)]
        
        # Forward benchmark
        benchmark = getattr(ns, "benchmark", None)
        if benchmark:
            a += ["--benchmark", benchmark]
    
    elif num == 24:
        # Script 24: Performance dashboard (uses --start-date)
        # No date argument needed - dashboard auto-loads all historical data
        
        # Forward account equity
        if hasattr(ns, "account_equity") and ns.account_equity is not None:
            a += ["--account-equity", str(ns.account_equity)]
        
        # Forward benchmark
        benchmark = getattr(ns, "benchmark", None)
        if benchmark:
            a += ["--benchmark", benchmark]
    
    # ── Scripts 16-21: Backtest & validation ──────────────────────────────────
    if num in (16, 17, 18):
        # Scripts 16-18: Backtest engine, walk-forward, monte carlo
        backtest_start = getattr(ns, "backtest_start", None)
        if backtest_start:
            a += ["--start-date", backtest_start]
        
        backtest_end = getattr(ns, "backtest_end", None)
        if backtest_end:
            a += ["--end-date", backtest_end]
        
        initial_equity = getattr(ns, "initial_equity", None)
        if initial_equity:
            a += ["--initial-equity", str(initial_equity)]
        
        max_positions = getattr(ns, "max_positions", None)
        if max_positions:
            a += ["--max-positions", str(max_positions)]
    
    elif num in (19, 20, 21):
        # Scripts 19-21: Validators and deployment decision
        # These read backtest results from files, minimal args needed
        pass

    # ── Universal: --dry-run ──────────────────────────────────────────────────
    # Script 15 only writes HTML reports
    if getattr(ns, "dry_run", False) and num not in (15,):
        a += ["--dry-run"]

    return a

# ─────────────────────────────────────────────────────────────────────────────
# STEP EXECUTOR
# ─────────────────────────────────────────────────────────────────────────────

_TAIL_LINES = 25   # last N lines captured for failure display

def run_step(
    num: int,
    extra_args: List[str],
    logger: logging.Logger,
    timeout: int = 1800,
) -> StepResult:
    """
    Execute a single script step as a subprocess.
    Returns a StepResult regardless of outcome.
    """
    label = SCRIPT_LABELS.get(num, f"Script {num}")
    script_path = find_script(num)

    if script_path is None:
        logger.warning(f"  Script {num} not found — SKIP")
        return StepResult(num, label, "SKIP", returncode=0)

    # Per-script timeout takes precedence over the global default
    effective_timeout = SCRIPT_TIMEOUTS.get(num, timeout)

    cmd = [sys.executable, str(script_path)] + extra_args
    cmd_str = " ".join(cmd)

    logger.debug(f"  CMD: {cmd_str}")
    if effective_timeout != timeout:
        logger.info(f"  Timeout override: {effective_timeout}s (global={timeout}s)")

    # ── Heartbeat thread for long-running scripts ─────────────────────────────
    # Prints a live progress line every 30s so the terminal doesn't look frozen.
    _heartbeat_stop  = threading.Event()
    _HEARTBEAT_SEC   = 60   # once per minute — avoids duplicate display

    def _heartbeat(label: str, stop: threading.Event, t_start: float) -> None:
        while not stop.wait(_HEARTBEAT_SEC):
            elapsed_s   = time.monotonic() - t_start
            elapsed_str = (
                f"{elapsed_s:.0f}s"        if elapsed_s < 120
                else f"{elapsed_s/60:.0f} min"
            )
            print(
                f"  {_c(CLR_DIM, f'  ⏳  {label} still running … {elapsed_str} elapsed')}",
                flush=True,
            )

    t0 = time.monotonic()

    # Only spin up heartbeat for scripts with extended timeouts
    hb_thread: Optional[threading.Thread] = None
    if num in SCRIPT_TIMEOUTS:
        hb_thread = threading.Thread(
            target=_heartbeat,
            args=(label, _heartbeat_stop, t0),
            daemon=True,
        )
        hb_thread.start()

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            cwd=SCRIPT_DIR,
        )
    except subprocess.TimeoutExpired:
        _heartbeat_stop.set()
        elapsed = time.monotonic() - t0
        logger.error(f"  Script {num} timed out after {effective_timeout}s")
        # Still apply soft-step logic on timeout
        status = "FAIL"
        stderr_detail = f"TIMEOUT after {effective_timeout}s"
        if num in SOFT_STEPS:
            cache_path  = SOFT_STEPS[num]
            resolved    = _resolve_cache_path(cache_path)
            cache_note  = (
                f"stale cache available at {resolved}"
                if resolved
                else f"no cache found (searched {SCRIPT_DIR / cache_path} and "
                     f"{SCRIPT_DIR.parent / cache_path})"
            )
            logger.warning(
                f"  Script {num} timed out — soft step, pipeline will continue "
                f"({cache_note})."
            )
            status = "WARN"
            stderr_detail += f" | {cache_note}"
        return StepResult(
            num, label, status,
            returncode=-1,
            duration_s=elapsed,
            cmd=cmd_str,
            stderr_tail=stderr_detail,
        )
    except Exception as exc:
        _heartbeat_stop.set()
        elapsed = time.monotonic() - t0
        logger.error(f"  Script {num} raised exception: {exc}")
        return StepResult(
            num, label, "FAIL",
            returncode=-2,
            duration_s=elapsed,
            cmd=cmd_str,
            stderr_tail=str(exc),
        )

    _heartbeat_stop.set()   # stop heartbeat on normal completion
    elapsed = time.monotonic() - t0
    rc = proc.returncode

    # Tail for diagnostics
    stdout_tail = "\n".join(proc.stdout.splitlines()[-_TAIL_LINES:])
    stderr_tail = "\n".join(proc.stderr.splitlines()[-_TAIL_LINES:])

    status = "OK" if rc == 0 else "FAIL"

    # Stream output to logger at appropriate level
    for line in proc.stdout.splitlines():
        logger.debug(f"  [{num}] {line}")
    for line in proc.stderr.splitlines():
        if rc == 0:
            logger.debug(f"  [{num}] STDERR: {line}")
        else:
            logger.warning(f"  [{num}] STDERR: {line}")

    return StepResult(
        num, label, status,
        returncode=rc,
        duration_s=elapsed,
        cmd=cmd_str,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
    )

# ─────────────────────────────────────────────────────────────────────────────
# DISPLAY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def banner(title: str, width: int = 72) -> None:
    line = "─" * width
    print(f"\n{_c(CLR_BOLD, line)}")
    print(_c(CLR_BOLD, f"  {title}"))
    print(_c(CLR_BOLD, line))

def step_header(num: int, label: str, total: int, idx: int) -> None:
    tag   = f"[{idx}/{total}]"
    title = f"Script {num:02d} — {label}"
    print(f"\n{_c(CLR_CYAN, tag)} {_c(CLR_BOLD, title)}")
    print(_c(CLR_DIM, "  " + "·" * 60))

def step_footer(result: StepResult) -> None:
    dur = f"{result.duration_s:.1f}s"
    if result.status == "OK":
        icon = _c(CLR_GREEN, "✔ PASSED")
    elif result.status == "SKIP":
        icon = _c(CLR_YELLOW, "⊘ SKIPPED")
    elif result.status == "WARN":
        icon = _c(CLR_YELLOW, "⚠ WARNING")
    else:
        icon = _c(CLR_RED, "✖ FAILED")
    print(f"  {icon}  ({dur})")

def print_failure_detail(result: StepResult) -> None:
    print(_c(CLR_RED, f"\n  ── Failure detail: Script {result.step_num} ──"))
    print(f"  Return code : {result.returncode}")
    print(f"  Command     : {result.cmd}")
    if result.stderr_tail:
        print(_c(CLR_DIM, "\n  STDERR (last lines):"))
        for line in result.stderr_tail.splitlines():
            print(f"    {line}")
    if result.stdout_tail:
        print(_c(CLR_DIM, "\n  STDOUT (last lines):"))
        for line in result.stdout_tail.splitlines()[-10:]:
            print(f"    {line}")

def print_summary(results: List[StepResult], total_s: float, mode: str) -> None:
    banner(f"Pipeline Summary — mode: {mode.upper()}")

    col_w = [6, 36, 10, 8]
    header = (
        f"  {'Step':<{col_w[0]}} {'Label':<{col_w[1]}} "
        f"{'Status':<{col_w[2]}} {'Duration':>{col_w[3]}}"
    )
    print(_c(CLR_BOLD, header))
    print("  " + "─" * (sum(col_w) + 6))

    passed = failed = skipped = 0
    for r in results:
        if r.status == "OK":
            st_str = _c(CLR_GREEN, f"{'OK':<{col_w[2]}}")
            passed += 1
        elif r.status in ("SKIP",):
            st_str = _c(CLR_YELLOW, f"{'SKIP':<{col_w[2]}}")
            skipped += 1
        elif r.status == "WARN":
            st_str = _c(CLR_YELLOW, f"{'WARN':<{col_w[2]}}")
            passed += 1
        else:
            st_str = _c(CLR_RED, f"{'FAIL':<{col_w[2]}}")
            failed += 1

        label_trunc = r.label[:col_w[1] - 1]
        print(
            f"  {r.step_num:<{col_w[0]}} {label_trunc:<{col_w[1]}} "
            f"{st_str} {r.duration_s:>{col_w[3]}.1f}s"
        )

    print("  " + "─" * (sum(col_w) + 6))
    total_status = (
        _c(CLR_GREEN, "ALL PASSED") if failed == 0
        else _c(CLR_RED, f"{failed} FAILED")
    )
    print(
        f"\n  Result  : {total_status}  "
        f"({passed} ok, {failed} failed, {skipped} skipped)"
    )
    print(f"  Elapsed : {total_s:.1f}s  ({total_s/60:.1f} min)")
    print()

# ─────────────────────────────────────────────────────────────────────────────
# PRE-FLIGHT CHECKS
# ─────────────────────────────────────────────────────────────────────────────

def preflight_check(steps: List[int], ns: argparse.Namespace, logger: logging.Logger) -> List[str]:
    """
    Validate that required arguments are present before any scripts run.
    Returns a list of error messages; empty list means all clear.
    """
    errors: List[str] = []

    date_required = set(range(4, 12)) | {14}
    if steps and any(s in date_required for s in steps):
        if not ns.as_of_date:
            errors.append(
                "--as-of-date YYYY-MM-DD is required for scripts 4-11, 14."
            )
        else:
            try:
                datetime.date.fromisoformat(ns.as_of_date)
            except ValueError:
                errors.append(
                    f"--as-of-date '{ns.as_of_date}' is not a valid YYYY-MM-DD date."
                )

    equity_required = {8, 11, 14}
    if steps and any(s in equity_required for s in steps):
        if not ns.account_equity:
            errors.append(
                "--account-equity EUR is required for scripts 8, 11, 14."
            )
        elif ns.account_equity <= 0:
            errors.append("--account-equity must be a positive number.")

    return errors

# ─────────────────────────────────────────────────────────────────────────────
# PIPELINE RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def run_pipeline(
    steps:    List[int],
    ns:       argparse.Namespace,
    logger:   logging.Logger,
    abort_on_fail: bool = True,
) -> Tuple[List[StepResult], int]:
    """
    Execute each step in order.

    Parameters
    ----------
    steps         : ordered list of script numbers to run
    ns            : parsed master namespace (carries all user flags)
    logger        : shared logger
    abort_on_fail : if True, stop the pipeline on first failure

    Returns
    -------
    results  : list of StepResult objects
    exit_code: 0 if all succeeded, 1 otherwise
    """
    results: List[StepResult] = []
    total   = len(steps)
    t_start = time.monotonic()

    for idx, num in enumerate(steps, start=1):
        label = SCRIPT_LABELS.get(num, f"Script {num}")
        step_header(num, label, total, idx)

        extra = build_args(num, ns)
        logger.info(f"Running Script {num:02d}: {label}")
        if extra:
            logger.debug(f"  Args: {' '.join(extra)}")

        result = run_step(num, extra, logger)
        results.append(result)
        step_footer(result)

        if result.status == "FAIL":
            print_failure_detail(result)
            if abort_on_fail:
                logger.error(
                    f"Aborting pipeline: Script {num} failed (rc={result.returncode})."
                )
                # Fill remaining steps as SKIP
                for remaining in steps[idx:]:
                    results.append(
                        StepResult(
                            remaining,
                            SCRIPT_LABELS.get(remaining, f"Script {remaining}"),
                            "SKIP",
                        )
                    )
                break

    elapsed = time.monotonic() - t_start
    failed  = sum(1 for r in results if r.status == "FAIL")
    return results, (0 if failed == 0 else 1)

# ─────────────────────────────────────────────────────────────────────────────
# ARGUMENT PARSING
# ─────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="00_trend_strategy_pipeline.py",
        description=textwrap.dedent("""\
            Master Pipeline Orchestrator — Architecture v3.2
            Runs all strategy scripts incrementally in the correct order.
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            EXAMPLES
            --------
            # One-time setup (full historical download):
            python scripts/00_trend_strategy_pipeline.py setup --force

            # Daily monitoring (fast):
            python scripts/00_trend_strategy_pipeline.py daily --account-equity 50000 --vix 17.2

            # Monthly rebalancing (no validation):
            python scripts/00_trend_strategy_pipeline.py monthly \\
                --as-of-date 2026-01-31 --account-equity 50000 --vix 18.5

            # Quarterly rebalancing + data quality audit:
            python scripts/00_trend_strategy_pipeline.py quarterly \\
                --as-of-date 2026-03-31 --account-equity 50000 --vix 16.8

            # Re-generate PDF report only:
            python scripts/00_trend_strategy_pipeline.py report --month 2026-01

            # Dry-run (no files written):
            python scripts/00_trend_strategy_pipeline.py monthly \\
                --as-of-date 2026-01-31 --account-equity 50000 --dry-run

            # Custom step selection:
            python scripts/00_trend_strategy_pipeline.py custom --steps 4,5,6 --as-of-date 2026-01-31

            # Custom with monthly behavior (consolidate-only Script 03):
            python scripts/00_trend_strategy_pipeline.py custom --as-monthly \\
                --steps 1,3,4,5,6,7,8,11,12 \\
                --as-of-date 2026-01-31 --account-equity 50000

            # Custom with quarterly behavior (full validation):
            python scripts/00_trend_strategy_pipeline.py custom --as-quarterly \\
                --steps 3,4,5,6,7 \\
                --as-of-date 2026-03-31 --account-equity 50000

            # Continue pipeline even after a failure:
            python scripts/00_trend_strategy_pipeline.py monthly \\
                --as-of-date 2026-01-31 --account-equity 50000 --no-abort
        """),
    )

    # ── Positional: mode ──────────────────────────────────────────────────────
    parser.add_argument(
        "mode",
        choices=["setup", "daily", "weekly", "monthly", "quarterly", "backtest", "validation", "analytics", "report", "custom"],
        help=(
            "Pipeline mode. "
            "setup=one-time initialization; "
            "daily=consolidate portfolio only (fast, ~1 min); "
            "weekly=Saturday run (consolidate all + stops + exits); "
            "monthly=end-of-month rebalancing (Saturday); "
            "quarterly=full rebalancing + data validation (slow); "
            "backtest=full validation pipeline (hours/days); "
            "validation=validate existing backtest results (fast); "
            "analytics=performance review only (fast); "
            "report=PDF only; "
            "custom=manual step selection."
        ),
    )

    # ── Date / equity ─────────────────────────────────────────────────────────
    parser.add_argument(
        "--as-of-date",
        metavar="YYYY-MM-DD",
        dest="as_of_date",
        default=None,
        help=(
            "Reference date for all analytical scripts. "
            "Required for modes: monthly, weekly, daily, custom (scripts 4-14). "
            "Defaults to today if omitted in 'daily' mode."
        ),
    )

    parser.add_argument(
        "--account-equity",
        metavar="EUR",
        dest="account_equity",
        type=float,
        default=None,
        help=(
            "Total account equity in EUR. "
            "Required for scripts 8, 11, 14."
        ),
    )

    # ── Optional analytical overrides ────────────────────────────────────────
    parser.add_argument(
        "--vix",
        metavar="LEVEL",
        type=float,
        default=None,
        help="Current VIX level for circuit-breaker checks in scripts 11 and 14.",
    )

    parser.add_argument(
        "--benchmark",
        metavar="SYMBOL",
        default="SPY.US",
        choices=["SPY.US", "ACWI.US", "QQQ.US"],
        help=(
            "Benchmark for performance attribution (scripts 22-24). "
            "Default: SPY.US (S&P 500). Options: ACWI.US (MSCI World), QQQ.US (Nasdaq-100)."
        ),
    )

    parser.add_argument(
        "--backtest-start",
        metavar="YYYY-MM-DD",
        default="2019-01-01",
        help="Backtest period start date (scripts 16-18). Default: 2019-01-01.",
    )

    parser.add_argument(
        "--backtest-end",
        metavar="YYYY-MM-DD",
        default="2024-12-31",
        help="Backtest period end date (scripts 16-18). Default: 2024-12-31.",
    )

    parser.add_argument(
        "--initial-equity",
        metavar="EUR",
        type=float,
        default=10000.0,
        help="Initial backtest equity in EUR (scripts 16-18). Default: 10000.",
    )

    parser.add_argument(
        "--max-positions",
        metavar="N",
        dest="max_positions",
        type=int,
        default=None,
        help=(
            "Override maximum number of portfolio positions. "
            "Used by scripts 8, 10, 11, 16-18."
        ),
    )

    parser.add_argument(
        "--month",
        metavar="YYYY-MM",
        default=None,
        help=(
            "Rebalancing month for the PDF report (script 12). "
            "Auto-derived from --as-of-date when not supplied."
        ),
    )

    parser.add_argument(
        "--chart-symbols",
        metavar="SYM,SYM,...",
        dest="chart_symbols",
        default=None,
        help=(
            "Comma-separated ticker list for script 15 charts "
            "(e.g. AAPL.US,MSFT.US). "
            "Omit to chart all qualified trends (--filter all)."
        ),
    )

    parser.add_argument(
        "--chart-lookback",
        metavar="DAYS",
        dest="chart_lookback",
        type=int,
        default=None,
        help=(
            "Lookback window in calendar days for script 15 charts "
            "(default: 200)."
        ),
    )

    parser.add_argument(
        "--override-circuit-breaker",
        metavar="RATIONALE",
        dest="override_circuit_breaker",
        default=None,
        help=(
            "Override a triggered circuit breaker in script 11. "
            "Requires a rationale string. "
            "Cannot override CRITICAL (data-staleness) breakers."
        ),
    )

    # ── Custom step selection ─────────────────────────────────────────────────
    parser.add_argument(
        "--steps",
        metavar="N,N,...",
        default=None,
        help=(
            "Comma-separated list of script numbers to run (mode=custom only). "
            "Example: --steps 4,5,6,7"
        ),
    )

    parser.add_argument(
        "--as-daily",
        action="store_true",
        dest="as_daily",
        default=False,
        help=(
            "Custom mode: inherit daily-mode behavior (Script 03 portfolio-only, fast). "
            "Without --steps, runs full daily pipeline. With --steps, runs only those scripts."
        ),
    )

    parser.add_argument(
        "--as-weekly",
        action="store_true",
        dest="as_weekly",
        default=False,
        help=(
            "Custom mode: inherit weekly-mode behavior (Script 03 consolidate-only, 12hr timeout). "
            "Without --steps, runs full weekly pipeline. With --steps, runs only those scripts."
        ),
    )

    parser.add_argument(
        "--as-monthly",
        action="store_true",
        dest="as_monthly",
        default=False,
        help=(
            "Custom mode: inherit monthly-mode behavior (Script 03 consolidate-only, 12hr timeout). "
            "Without --steps, runs full monthly pipeline. With --steps, runs only those scripts."
        ),
    )

    parser.add_argument(
        "--as-quarterly",
        action="store_true",
        dest="as_quarterly",
        default=False,
        help=(
            "Custom mode: inherit quarterly-mode behavior (Script 03 with validation, 4hr timeout). "
            "Without --steps, runs full quarterly pipeline. With --steps, runs only those scripts."
        ),
    )

    parser.add_argument(
        "--as-backtest",
        action="store_true",
        dest="as_backtest",
        default=False,
        help=(
            "Custom mode: inherit backtest-mode behavior (backtest arguments). "
            "Without --steps, runs full backtest pipeline. With --steps, runs only those scripts."
        ),
    )

    parser.add_argument(
        "--as-validation",
        action="store_true",
        dest="as_validation",
        default=False,
        help=(
            "Custom mode: inherit validation-mode behavior. "
            "Without --steps, runs full validation pipeline. With --steps, runs only those scripts."
        ),
    )

    # ── Execution control ─────────────────────────────────────────────────────
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Pass --force to Script 01 (skip confirmation prompts).",
    )

    parser.add_argument(
        "--skip-install",
        dest="skip_install",
        action="store_true",
        default=False,
        help=(
            "Skip automatic pip installation of missing packages. "
            "Pipeline will fail immediately if a required package is absent."
        ),
    )

    parser.add_argument(
        "--force-friday",
        dest="force_friday",
        action="store_true",
        default=False,
        help=(
            "Force trailing-stop recalculation in Script 09 "
            "even if today is not Friday."
        ),
    )

    parser.add_argument(
        "--check-rotation",
        dest="check_rotation",
        action="store_true",
        default=True,
        help="Enable rotation-exit check in Script 10 (default: on).",
    )

    parser.add_argument(
        "--no-abort",
        dest="abort_on_fail",
        action="store_false",
        default=True,
        help=(
            "Continue running subsequent steps even when a step fails. "
            "By default the pipeline aborts on first failure."
        ),
    )

    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help=(
            "Pass --dry-run to all supporting scripts. "
            "Computes everything but writes no output files."
        ),
    )

    parser.add_argument(
        "--log-dir",
        dest="log_dir",
        metavar="PATH",
        default="logs",
        help="Directory for pipeline log files (default: logs/).",
    )

    parser.add_argument(
        "--timeout",
        metavar="SECONDS",
        type=int,
        default=1800,
        help="Per-script subprocess timeout in seconds (default: 1800 = 30 min).",
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s v{VERSION} — Architecture v3.2",
    )

    return parser

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = build_parser()
    ns     = parser.parse_args()

    # ── Log directory ─────────────────────────────────────────────────────────
    log_dir = Path(ns.log_dir)
    logger  = setup_logging(log_dir)

    # ── Banner ────────────────────────────────────────────────────────────────
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    banner(
        f"Multi-Asset Trend Following Pipeline  v{VERSION}",
        width=72,
    )
    print(f"  Mode      : {_c(CLR_BOLD, ns.mode.upper())}")
    print(f"  Started   : {now_str}")
    if ns.as_of_date:
        print(f"  As-of     : {ns.as_of_date}")
    if ns.account_equity:
        print(f"  Equity    : €{ns.account_equity:,.0f}")
    if ns.vix is not None:
        print(f"  VIX       : {ns.vix}")
    if ns.dry_run:
        print(f"  {_c(CLR_YELLOW, '⚠  DRY-RUN mode — no files will be written')}")
    print()

    logger.info(f"Mode={ns.mode}  as_of_date={ns.as_of_date}  "
                f"account_equity={ns.account_equity}  vix={ns.vix}  "
                f"dry_run={ns.dry_run}")

    # ── Dependency check & auto-install ───────────────────────────────────────
    banner("Dependency Check", width=72)
    still_missing = check_and_install_deps(logger, auto_install=not ns.skip_install)
    if still_missing:
        logger.error(
            f"Cannot proceed — packages still missing after install attempt: "
            f"{still_missing}. "
            f"Run manually: pip install {' '.join(still_missing)}"
        )
        print(
            _c(CLR_RED,
               f"\n  ✖ Missing packages could not be installed: {still_missing}\n"
               f"  Run: pip install {' '.join(still_missing)}\n")
        )
        return 2
    print(f"  {_c(CLR_GREEN, '✔ All dependencies satisfied.')}\n")

    # ── Resolve step list ─────────────────────────────────────────────────────
    if ns.mode == "custom":
        # Determine effective mode for Script 03 behavior
        if ns.as_quarterly:
            ns.effective_mode = "quarterly"
            SCRIPT_TIMEOUTS[3] = 14400  # 4 hours for validation
        elif ns.as_monthly:
            ns.effective_mode = "monthly"
            SCRIPT_TIMEOUTS[3] = 43200  # 12 hours for consolidation
        elif ns.as_weekly:
            ns.effective_mode = "weekly"
            SCRIPT_TIMEOUTS[3] = 43200  # 12 hours for consolidation
        elif ns.as_daily:
            ns.effective_mode = "daily"
            SCRIPT_TIMEOUTS[3] = 600  # 10 minutes (portfolio only, very fast)
        elif ns.as_backtest:
            ns.effective_mode = "backtest"
        elif ns.as_validation:
            ns.effective_mode = "validation"
        else:
            ns.effective_mode = None  # Generic custom mode

        # Resolve step list
        if ns.steps:
            # Explicit steps provided
            try:
                steps = [int(s.strip()) for s in ns.steps.split(",")]
            except ValueError:
                logger.error(f"--steps '{ns.steps}' must be comma-separated integers.")
                return 2
            unknown = [s for s in steps if s not in SCRIPT_MAP]
            if unknown:
                logger.error(f"Unknown script number(s): {unknown}")
                return 2
        elif ns.effective_mode:
            # No --steps, but --as-MODE specified: use that mode's default pipeline
            steps = PIPELINES[ns.effective_mode]
            logger.info(
                f"Custom mode with --as-{ns.effective_mode}: "
                f"using default {ns.effective_mode} pipeline"
            )
        else:
            # No --steps and no --as-MODE
            logger.error(
                "mode=custom requires either --steps N,N,... or --as-MODE flag "
                "(--as-daily, --as-weekly, --as-monthly, --as-quarterly, --as-backtest, --as-validation)."
            )
            return 2
    elif ns.mode == "setup":
        # Force full modes for initial one-time rebuild
        ns.download_mode     = "initial"
        ns.consolidate_mode  = "full"
        ns.effective_mode    = "setup"
        steps = PIPELINES["setup"]
    elif ns.mode == "analytics":
        # Analytics: performance review only (no rebalancing)
        ns.effective_mode = "analytics"
        steps = PIPELINES["analytics"]
    elif ns.mode == "backtest":
        # Backtest: full validation pipeline (can take hours/days).
        # Override per-script timeouts — no script in this pipeline has a
        # fixed ceiling.  Assign generous-but-finite limits so a genuine hang
        # is still caught (e.g. infinite loop, deadlock) while a legitimate
        # long run completes normally.
        #
        #   Script 16 — Backtest engine         :  2 h  (observed ~77 s; headroom for large universes)
        #   Script 17 — Walk-forward optimizer  :  8 h  (972 combos × 6 windows; was failing at 30 min)
        #   Script 18 — Monte Carlo simulator   :  4 h  (10 000 simulations over equity curve)
        #   Script 19 — Backtest validator      :  2 h  (statistics + report generation)
        #   Script 20 — OOS validator           :  2 h  (statistics + report generation)
        #   Script 21 — Deployment decision     :  1 h  (lightweight decision logic)
        SCRIPT_TIMEOUTS[16] = 7_200    # 2 hours
        SCRIPT_TIMEOUTS[17] = 28_800   # 8 hours  ← was hitting the 1800 s global default
        SCRIPT_TIMEOUTS[18] = 14_400   # 4 hours
        SCRIPT_TIMEOUTS[19] = 7_200    # 2 hours
        SCRIPT_TIMEOUTS[20] = 7_200    # 2 hours
        SCRIPT_TIMEOUTS[21] = 3_600    # 1 hour
        ns.effective_mode = "backtest"
        steps = PIPELINES["backtest"]
    elif ns.mode == "validation":
        # Validation: validate existing backtest results only
        ns.effective_mode = "validation"
        steps = PIPELINES["validation"]
    else:
        # report mode
        ns.effective_mode = ns.mode
        steps = PIPELINES[ns.mode]

    # ── Default as-of-date for daily mode ────────────────────────────────────
    if ns.mode == "daily" and not ns.as_of_date:
        ns.as_of_date = datetime.date.today().isoformat()
        logger.info(f"No --as-of-date supplied; defaulting to today: {ns.as_of_date}")

    # ── Pre-flight validation ─────────────────────────────────────────────────
    errors = preflight_check(steps, ns, logger)
    if errors:
        for e in errors:
            logger.error(f"Pre-flight: {e}")
            print(_c(CLR_RED, f"  ✖ {e}"))
        print()
        print("  Run with --help for usage details.")
        return 2

    # ── Log resolved pipeline ─────────────────────────────────────────────────
    print(_c(CLR_BOLD, f"  Steps to execute ({len(steps)}):"))
    for n in steps:
        script_path = find_script(n)
        found_str   = str(script_path.name) if script_path else _c(CLR_RED, "NOT FOUND")
        print(f"    {n:>2}. {SCRIPT_LABELS.get(n, '?'):<40}  {found_str}")
    print()

    # Warn about missing scripts (don't abort — they'll be SKIP'd)
    missing = [n for n in steps if find_script(n) is None]
    if missing:
        logger.warning(
            f"Scripts not found (will be skipped): {missing}. "
            "Ensure script files are in the same directory or in ./scripts/."
        )

    # ── Execute ───────────────────────────────────────────────────────────────
    t_pipeline_start = time.monotonic()

    results, exit_code = run_pipeline(
        steps         = steps,
        ns            = ns,
        logger        = logger,
        abort_on_fail = ns.abort_on_fail,
    )

    total_elapsed = time.monotonic() - t_pipeline_start

    # ── Summary ───────────────────────────────────────────────────────────────
    print_summary(results, total_elapsed, ns.mode)

    failed_steps = [r for r in results if r.status == "FAIL"]
    if failed_steps:
        logger.error(
            f"Pipeline FAILED. Failed steps: "
            f"{[r.step_num for r in failed_steps]}"
        )
        print(
            _c(CLR_RED,
               "  Pipeline completed with errors. "
               "Review the log for details.\n"
               "  Tip: fix the failing step and re-run from that script, or\n"
               "       use --no-abort to run all steps regardless.\n")
        )
    else:
        logger.info("Pipeline completed successfully.")
        if ns.mode == "monthly" and not ns.dry_run:
            print(_c(CLR_GREEN, "  ✔ Rebalancing pipeline complete.\n"))
            print(
                "  Next steps (human-in-the-loop):\n"
                "    1. Review the PDF report in reports/rebalancing/\n"
                "    2. Validate all exit and entry recommendations\n"
                "    3. Execute orders at market open on the execution date\n"
                "    4. Log each fill: python 13_log_execution.py --help\n"
                "    5. Confirm stop-loss orders are live with your broker\n"
            )
        elif ns.mode == "daily" and not ns.dry_run:
            print(_c(CLR_GREEN, "  ✔ Daily monitoring complete.\n"))
            print(
                "  Review report in reports/monitoring/\n"
                "  Act on any STOP-HIT or TREND-REVERSAL alerts immediately.\n"
            )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
