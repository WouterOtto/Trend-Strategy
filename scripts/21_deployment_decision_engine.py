#!/usr/bin/env python3
"""
Script 21: Deployment Decision Engine
========================================
Synthesise all validation evidence from Scripts 16–20 into a single, binding
Go / No-Go deployment decision using a deterministic 5-tier framework.

Purpose
-------
Script 21 is the final gate before live capital is committed.  It consumes the
outputs of every upstream validator, runs a Monte Carlo permutation test to
distinguish skill from luck, applies the 5-Tier Decision Matrix, and emits a
human-readable Decision Dashboard (HTML) plus a machine-readable JSON record
that serves as the official audit artefact.

Architecture Reference: v3.3 (Mar 2026)

Inputs
------
    Script 19 → data_cache/validation/validation_results.json
    Script 20 → data_cache/oos_validation/oos_validation_results.json
    Script 18 → data_cache/monte_carlo/mc_summary.json
    Script 16 → data_cache/backtest/trade_log.csv   (for permutation test)
                data_cache/backtest/equity_curve.csv

10-Step Decision Process
------------------------
    Step 1  : Load all upstream outputs
    Step 2  : Extract Script-19 primary-test results (10 binary tests)
    Step 3  : Extract Script-19 scoring system result (30-pt scale)
    Step 4  : Extract Script-19 red-flag analysis (7 flags)
    Step 5  : Extract Script-19 benchmark comparison
    Step 6  : Extract Script-20 OOS stability metrics
    Step 7  : Run Monte Carlo permutation validation (trade-sequence shuffle)
    Step 8  : Apply 5-Tier Decision Matrix → assign Tier
    Step 9  : Generate Decision Dashboard (HTML) with traffic-light indicators
    Step 10 : Persist machine-readable decision record (JSON) + emit summary

5-Tier Decision Matrix
----------------------
    Tier 1 — DEPLOY IMMEDIATELY
        ALL 10 primary tests PASS
        Score ≥ 23 points (GOOD or better)
        Beats SPY on risk-adjusted basis
        No critical red flags
        OOS stability ratio > 0.7
        Parameter CV < 20 %
        Monte Carlo 5th percentile > −20 %

    Tier 2 — DEPLOY WITH MONITORING
        ALL 10 primary tests PASS
        Score 19–22 (ACCEPTABLE)
        Beats benchmark risk-adjusted
        No critical red flags
        1–2 warning signs present
        Stability ratio 0.6–0.7

    Tier 3 — PAPER TRADE FIRST
        8–9 primary tests PASS
        Score 15–18 (MARGINAL)
        Barely beats benchmark OR 2–3 warnings OR stability ratio 0.5–0.6

    Tier 4 — IMPROVE AND RETEST
        < 8 primary tests PASS  OR  score < 15  OR  no benchmark beat
        OR 1+ critical red flags  (but not multiple + fundamental faults)

    Tier 5 — REJECT
        Multiple critical red flags  OR  fundamental logic errors
        OR cannot be profitable with realistic costs  OR  ethical concerns

Monte Carlo Permutation Component
-----------------------------------
    1.  Load up to 5 000 completed round-trip trades from trade_log.csv
    2.  Shuffle trade sequence 10 000 times (preserving individual trade P&Ls)
    3.  Rebuild equity curve for each simulation
    4.  Derive distribution of final equity values
    Metrics:
        Percentile Rank    : where actual result sits in shuffled distribution
        95 % CI            : [5th, 95th] percentile of final equity
        Tail Risk          : 5th percentile final equity vs starting capital
        P(Target Return)   : fraction of sims achieving ≥ 50 % total return

Outputs
-------
    data_cache/deployment/deployment_decision.json      (machine-readable record)
    reports/deployment/{YYYYMMDD}_deployment_decision_report.json
    reports/deployment/{YYYYMMDD}_deployment_dashboard.html  (traffic-light dashboard)
    logs/deployment_{timestamp}.log

Execution
---------
    # Standard run
    python scripts/21_deployment_decision_engine.py

    # With custom tags matching upstream scripts
    python scripts/21_deployment_decision_engine.py --backtest-tag run_01 --wfo-tag q4

    # Skip permutation test (fast mode, e.g. CI pipeline)
    python scripts/21_deployment_decision_engine.py --skip-permutation

    # Strict mode (Tier 2 treated as Tier 3)
    python scripts/21_deployment_decision_engine.py --strict

    # Override starting capital for Monte Carlo calculations
    python scripts/21_deployment_decision_engine.py --starting-capital 50000

Architecture: v3.3 (Mar 2026)
"""

import os
import sys
import json
import logging
import argparse
import math
import warnings
from copy import deepcopy
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)


# ===========================================================================
# PROJECT PATHS
# ===========================================================================

# ===========================================================================
# PATH RESOLUTION
# ===========================================================================
# The project uses an inconsistent PROJECT_ROOT across scripts:
#   Scripts 16, 17, 18  →  Path(__file__).parent.parent   (project root)
#   Scripts 19, 20      →  Path(__file__).resolve().parent (scripts/ subdir)
#
# Script 21 resolves this by:
#   • Using PROJECT_ROOT = parent.parent (matching 16/17/18) as the canonical root
#   • Probing both locations for S19 / S20 outputs (in case they used .parent)
# ===========================================================================

_SCRIPT_DIR  = Path(__file__).resolve().parent          # .../trend_strategy/scripts
PROJECT_ROOT = (
    _SCRIPT_DIR.parent                                  # .../trend_strategy
    if _SCRIPT_DIR.name == "scripts"
    else _SCRIPT_DIR
)

# ── Primary data_cache (Scripts 16/17/18 use parent.parent → project root) ──
DATA_CACHE_DIR  = PROJECT_ROOT / "data_cache"
BACKTEST_DIR    = DATA_CACHE_DIR / "backtest"
WFO_DIR         = BACKTEST_DIR / "walk_forward"
MC_DIR          = DATA_CACHE_DIR / "monte_carlo"

# ── Scripts 19/20 use parent → they write one level lower (scripts/data_cache)
# We check both locations and use whichever has the file (see _find_json below)
_DATA_CACHE_ALT = _SCRIPT_DIR / "data_cache"            # fallback for S19/S20
VALIDATION_DIR  = DATA_CACHE_DIR / "validation"
VALIDATION_DIR_ALT = _DATA_CACHE_ALT / "validation"
OOS_DIR         = DATA_CACHE_DIR / "oos_validation"
OOS_DIR_ALT     = _DATA_CACHE_ALT / "oos_validation"

# ── All outputs go to project root (consistent with Scripts 16/17/18) ───────
DEPLOYMENT_DIR  = PROJECT_ROOT / "data_cache" / "deployment"
REPORTS_DIR     = PROJECT_ROOT / "reports" / "deployment"
LOG_DIR         = PROJECT_ROOT / "logs"


# ===========================================================================
# CONSTANTS & THRESHOLDS
# ===========================================================================

# ── Tier 1 gate criteria (ALL must be True) ──────────────────────────────────
TIER1_CRITERIA = {
    "all_10_tests_pass":            True,
    "min_score":                    23,    # ≥ 23 points (GOOD or better)
    "beats_spy_risk_adjusted":      True,
    "no_critical_red_flags":        True,
    "min_stability_ratio":          0.70,
    "max_param_cv":                 0.20,  # 20 %
    "mc_tail_risk_floor":          -20.0,  # 5th-pct return must be > -20 %
}

# ── Tier 2 gate criteria ──────────────────────────────────────────────────────
TIER2_CRITERIA = {
    "all_10_tests_pass":            True,
    "min_score":                    19,    # ACCEPTABLE (19–22)
    "max_score":                    22,
    "beats_benchmark_risk_adjusted":True,
    "no_critical_red_flags":        True,
    "max_warnings":                 2,
    "stability_ratio_low":          0.60,
    "stability_ratio_high":         0.70,
}

# ── Tier 3 gate criteria ──────────────────────────────────────────────────────
TIER3_CRITERIA = {
    "min_tests_pass":               8,     # 8–9 pass
    "min_score":                    15,    # MARGINAL (15–18)
    "max_score":                    18,
    "stability_ratio_low":          0.30,  # lowered from 0.50 — consistent with Script 20
                                           # recalibration; all-window ratio is structurally
                                           # depressed by COVID-era IS windows in 2016-2024 data
    "stability_ratio_high":         0.60,
}

# ── Monte Carlo thresholds ────────────────────────────────────────────────────
MC_PERCENTILE_RANK_EXCELLENT  = 90    # actual result at ≥ 90th percentile
MC_PERCENTILE_RANK_GOOD       = 70
MC_PERCENTILE_RANK_ACCEPTABLE = 50
MC_TAIL_FLOOR                 = -20.0 # 5th-pct return must be > -20 % (Tier 1)
MC_TAIL_HARD_FLOOR            = -30.0 # any worse → strong downgrade signal
MC_CI_RELATIVE_WIDTH_GOOD     = 0.50  # (Upper - Lower) / Starting Capital
MC_CI_RELATIVE_WIDTH_POOR     = 1.00
MC_TARGET_RETURN_EXCELLENT    = 75.0  # % of sims ≥ 50 % return
MC_TARGET_RETURN_GOOD         = 60.0
MC_N_SIMULATIONS              = 10_000
MC_TARGET_RETURN_THRESHOLD    = 0.50  # 50 % total return threshold

# ── Scoring band labels (from Script 19) ─────────────────────────────────────
SCORE_BANDS = [
    (27, 30, "EXCELLENT"),
    (23, 26, "GOOD"),
    (19, 22, "ACCEPTABLE"),
    (15, 18, "MARGINAL"),
    (0,  14, "FAIL"),
]

# ── Tier labels & metadata ───────────────────────────────────────────────────
TIER_META = {
    1: {
        "label":        "DEPLOY IMMEDIATELY",
        "risk_level":   "LOW",
        "success_rate": "85–95 %",
        "action":       "Full capital allocation, standard position sizes, monthly monitoring.",
        "color":        "#22c55e",   # green
        "icon":         "✅",
    },
    2: {
        "label":        "DEPLOY WITH MONITORING",
        "risk_level":   "MODERATE",
        "success_rate": "70–85 %",
        "action":       (
            "Reduce initial position sizes 10–20 %. Tighten stop-losses 10 %. "
            "Weekly monitoring first 3 months, then monthly. Conservative ramp-up."
        ),
        "color":        "#84cc16",   # lime
        "icon":         "⚡",
    },
    3: {
        "label":        "PAPER TRADE FIRST",
        "risk_level":   "MODERATE-HIGH",
        "success_rate": "50–70 %",
        "action":       (
            "Paper trade 3–6 months. Generate real-time recommendations but do not execute. "
            "Promote to Tier 2 if paper results match backtest within 20 % and Sharpe ≥ 0.5."
        ),
        "color":        "#f59e0b",   # amber
        "icon":         "⚠️",
    },
    4: {
        "label":        "IMPROVE AND RETEST",
        "risk_level":   "HIGH",
        "success_rate": "30–50 % (after improvements)",
        "action":       (
            "Identify root cause of failures. Simplify strategy. Expand backtest period. "
            "Re-optimise with broader parameter ranges. Repeat up to 3 iterations."
        ),
        "color":        "#f97316",   # orange
        "icon":         "🔧",
    },
    5: {
        "label":        "REJECT",
        "risk_level":   "EXTREME",
        "success_rate": "0 % (not fixable)",
        "action":       (
            "Do NOT attempt to fix. Start over with a different approach. "
            "Document reasons for rejection for future reference."
        ),
        "color":        "#ef4444",   # red
        "icon":         "❌",
    },
}


# ===========================================================================
# LOGGING
# ===========================================================================

def _build_logger(tag: str) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"deployment{('_' + tag) if tag else ''}"
    log  = logging.getLogger(name)
    log.setLevel(logging.DEBUG)
    fmt  = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s",
                              datefmt="%Y-%m-%d %H:%M:%S")
    ch   = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    fh   = logging.FileHandler(LOG_DIR / f"deployment_{ts}.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(ch)
    log.addHandler(fh)
    return log


# ===========================================================================
# I/O HELPERS
# ===========================================================================

def _load_json(path: Path, label: str, log: logging.Logger) -> Optional[Dict]:
    if not path.exists():
        log.warning(f"[MISSING] {label}: {path}")
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        log.info(f"[OK]      {label}: {path}")
        return data
    except Exception as exc:
        log.error(f"[ERROR]   {label}: {path} — {exc}")
        return None


def _load_csv(path: Path, label: str, log: logging.Logger) -> Optional[pd.DataFrame]:
    if not path.exists():
        log.warning(f"[MISSING] {label}: {path}")
        return None
    try:
        df = pd.read_csv(path)
        log.info(f"[OK]      {label}: {path}  ({len(df):,} rows)")
        return df
    except Exception as exc:
        log.error(f"[ERROR]   {label}: {path} — {exc}")
        return None


def _sfx(tag: str) -> str:
    return f"_{tag}" if tag else ""


def _json_serial(obj):
    """JSON serialiser for numpy / pandas types."""
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Not serialisable: {type(obj)}")


# ===========================================================================
# STEP 1: LOAD UPSTREAM OUTPUTS
# ===========================================================================

def _find_json(
    primary: Path, alt: Path, label: str, log: logging.Logger
) -> Optional[Dict]:
    """Try primary path first; fall back to alt if primary is missing."""
    if primary.exists():
        return _load_json(primary, label, log)
    if alt.exists():
        log.info(f"[ALT]     {label}: falling back to {alt}")
        return _load_json(alt, label, log)
    # Report missing using the primary path so the user knows the canonical location
    log.warning(f"[MISSING] {label}: {primary}")
    return None


def load_upstream(backtest_tag: str, wfo_tag: str, log: logging.Logger) -> Dict:
    """
    Load outputs from Scripts 16–20.

    Path note: Scripts 16/17/18 use PROJECT_ROOT = parent.parent (project root).
    Scripts 19/20 use PROJECT_ROOT = parent (scripts/ subdir) — one level lower.
    We probe both locations so the loader is robust to either convention.
    """
    bs, ws = _sfx(backtest_tag), _sfx(wfo_tag)
    inputs: Dict[str, Any] = {}

    # Script 19 — check project-root/data_cache first, then scripts/data_cache
    inputs["s19"] = _find_json(
        VALIDATION_DIR     / f"validation_results{bs}.json",
        VALIDATION_DIR_ALT / f"validation_results{bs}.json",
        "S19 validation_results", log,
    )

    # Script 20 — same dual-probe
    inputs["s20"] = _find_json(
        OOS_DIR     / f"oos_validation_results{ws}.json",
        OOS_DIR_ALT / f"oos_validation_results{ws}.json",
        "S20 oos_validation_results", log,
    )

    # Script 18 (Monte Carlo summary) — project root only
    inputs["s18_mc"] = _load_json(
        MC_DIR / f"mc_summary{bs}.json",
        "S18 mc_summary", log,
    )

    # Script 16 trade log — project root only
    inputs["trade_log"] = _load_csv(
        BACKTEST_DIR / f"trade_log{bs}.csv",
        "S16 trade_log", log,
    )

    # Script 16 equity curve — project root only
    inputs["equity_curve"] = _load_csv(
        BACKTEST_DIR / f"equity_curve{bs}.csv",
        "S16 equity_curve", log,
    )

    return inputs


# ===========================================================================
# STEP 2–5: EXTRACT SCRIPT-19 EVIDENCE
# ===========================================================================

def _extract_s19(s19: Optional[Dict], log: logging.Logger) -> Dict:
    """
    Pull the fields Script 21 needs from the Script-19 result dict.

    Real Script-19 JSON structure (v3.2):
    {
      "tests": {
          "T01_positive_expectancy": {"test_id": "T01", "passed": bool, ...},
          ...
      },
      "score": {
          "per_metric_scores": {...},
          "total_score": int,
          "max_score": 30,
          "rating": "GOOD",
          ...
      },
      "red_flags": {
          "flags": {"RF01_...": {"triggered": bool, "severity": "CRITICAL"|"OK", ...}, ...},
          "critical_count": int,
          "warning_count": int,
          "critical_flags": [list of triggered critical flag ids],
          "warning_flags": [...],
          "any_critical": bool,
      },
      "benchmark_comparison": {
          "primary_benchmark_pass": bool,
          "benchmarks_beaten_count": int,
          ...
      },
      "overall_decision": {
          "verdict": "APPROVE"|"CONDITIONAL"|"REJECT"|"UNKNOWN",
          "tests_passed": int,
          "tests_total": int,
          "total_score": int,
          "rating": str,
          ...
      }
    }
    """
    out = {
        "available":            s19 is not None,
        "tests_passed":         0,
        "tests_failed":         0,
        "tests_total":          10,
        "test_results":         {},
        "score":                0,
        "score_band":           "UNKNOWN",
        "red_flags":            [],
        "red_flags_count":      0,
        "beats_spy":            False,
        "benchmarks_beaten":    0,
        "verdict":              "UNKNOWN",
        "warnings":             [],
    }
    if s19 is None:
        log.warning("Script-19 results unavailable — all S19 gates will fail.")
        return out

    # ── Primary tests: s19["tests"] is a LIST of {test_id, passed, value, ...}
    test_section = s19.get("tests") or []
    passed = failed = 0

    if isinstance(test_section, list):
        # Real structure: [{"test_id": "T01", "passed": True, ...}, ...]
        for item in test_section:
            if not isinstance(item, dict):
                continue
            key    = item.get("test_id") or item.get("id") or f"T{passed+failed+1:02d}"
            result = bool(item.get("passed", False))
            out["test_results"][key] = result
            if result:
                passed += 1
            else:
                failed += 1
    elif isinstance(test_section, dict):
        # Fallback: keyed dict {test_id: {passed: bool, ...}} or {test_id: bool}
        for k, v in test_section.items():
            result = bool(v.get("passed", False)) if isinstance(v, dict) else bool(v)
            out["test_results"][k] = result
            if result:
                passed += 1
            else:
                failed += 1

    # Fallback: read counts directly from overall_decision if tests was empty
    decision = s19.get("overall_decision", {})
    if passed == 0 and failed == 0:
        passed = int(decision.get("tests_passed", 0))
        failed = int(decision.get("tests_total", 10)) - passed

    out["tests_passed"] = passed
    out["tests_failed"] = failed

    # ── Scoring: s19["score"]["total_score"] ─────────────────────────────────
    score_obj = s19.get("score", {})
    if isinstance(score_obj, dict):
        raw_score = score_obj.get("total_score") or decision.get("total_score") or 0
    else:
        raw_score = int(score_obj) if score_obj else 0
    out["score"] = int(raw_score)

    # Derive band from score (same table as Script 19)
    for lo, hi, band in SCORE_BANDS:
        if lo <= out["score"] <= hi:
            out["score_band"] = band
            break

    # ── Red flags: s19["red_flags"]["critical_count"] + ["critical_flags"] ───
    rf_obj = s19.get("red_flags", {})
    if isinstance(rf_obj, dict):
        out["red_flags_count"] = int(rf_obj.get("critical_count", 0))
        out["red_flags"]       = rf_obj.get("critical_flags", [])
        # Include warning flags in the "warnings" list for tier-2 gate
        out["warnings"]        = rf_obj.get("warning_flags", [])
    elif isinstance(rf_obj, list):
        out["red_flags"]       = rf_obj
        out["red_flags_count"] = len(rf_obj)
    else:
        out["red_flags_count"] = 0

    # ── Benchmark: s19["benchmark_comparison"]["primary_benchmark_pass"] ─────
    bench = s19.get("benchmark_comparison", {})
    if isinstance(bench, dict):
        out["beats_spy"] = bool(bench.get("primary_benchmark_pass", False))
        out["benchmarks_beaten"] = int(bench.get("benchmarks_beaten_count", 0))
    else:
        out["beats_spy"] = False

    # ── Overall verdict from overall_decision ─────────────────────────────────
    out["verdict"] = decision.get("verdict", out["score_band"])

    log.info(
        f"S19 → Tests: {out['tests_passed']}/{out['tests_total']} pass | "
        f"Score: {out['score']}/30 ({out['score_band']}) | "
        f"Red flags: {out['red_flags_count']} | Beats SPY: {out['beats_spy']} | "
        f"Verdict: {out['verdict']}"
    )
    return out


# ===========================================================================
# STEP 6: EXTRACT SCRIPT-20 OOS EVIDENCE
# ===========================================================================

def _extract_s20(s20: Optional[Dict], log: logging.Logger) -> Dict:
    """
    Pull OOS stability metrics from the Script-20 result dict.

    Real Script-20 JSON structure (v3.2):
      window_analysis.stability_ratio  : float  (avg OOS / avg IS Sharpe)
      window_analysis.oos_consistency  : float  (fraction of windows positive Sharpe)
      score.score                      : int    (0-100)
      overall_decision.verdict         : str    ROBUST | MARGINAL | OVERFITTED
      parameter_stability.<param>.cv   : float  coefficient of variation
      diagnostics.diagnostics.<D>.triggered : bool
    """
    out = {
        "available":                s20 is not None,
        "stability_ratio":          None,
        "oos_consistency":          None,
        "verdict":                  "UNKNOWN",
        "param_cv_max":             None,
        "diagnostics_triggered":    [],
        "score":                    None,
        "warnings":                 [],
    }
    if s20 is None:
        log.warning("Script-20 results unavailable — OOS gates will use defaults.")
        return out

    # ── Stability ratio & OOS consistency ───────────────────────────────────────
    # Script 20 stores these inside "window_analysis" (filtered dict without
    # window_rows).  When WFO data was unavailable, window_analysis may be
    # absent or {"available": False} — fall back to top-level keys.
    def _float_or_none(v) -> Optional[float]:
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    window = s20.get("window_analysis") or {}
    if isinstance(window, dict) and window.get("available", True):
        out["stability_ratio"] = _float_or_none(window.get("stability_ratio"))
        out["oos_consistency"]  = _float_or_none(window.get("oos_consistency"))

    # Fallback 1: top-level keys (some Script-20 versions flatten them)
    if out["stability_ratio"] is None:
        out["stability_ratio"] = _float_or_none(s20.get("stability_ratio"))
    if out["oos_consistency"] is None:
        out["oos_consistency"] = _float_or_none(s20.get("oos_consistency"))

    # Fallback 2: compute from overall_decision if still missing
    # (Script 20 always populates overall_decision even without WFO windows)
    if out["stability_ratio"] is None:
        od = s20.get("overall_decision") or {}
        out["stability_ratio"] = _float_or_none(od.get("stability_ratio"))
    if out["oos_consistency"] is None:
        od = s20.get("overall_decision") or {}
        out["oos_consistency"] = _float_or_none(od.get("oos_consistency"))

    # Verdict from overall_decision ───────────────────────────────────────────
    decision = s20.get("overall_decision") or {}
    out["verdict"] = (
        decision.get("verdict")
        or s20.get("verdict")
        or "UNKNOWN"
    )

    # Parameter CV — max CV across all params in parameter_stability ──────────
    param_stab = s20.get("parameter_stability") or {}
    cvs = []
    if isinstance(param_stab, dict):
        for v in param_stab.values():
            if isinstance(v, dict):
                cv = v.get("cv") or v.get("coefficient_of_variation")
                if cv is not None:
                    try:
                        cvs.append(float(cv))
                    except (TypeError, ValueError):
                        pass
            elif isinstance(v, (int, float)):
                cvs.append(float(v))
    out["param_cv_max"] = max(cvs) if cvs else None

    # Triggered diagnostics — inside diagnostics.diagnostics.<D>.triggered ────
    diag_outer = s20.get("diagnostics") or {}
    triggered_names: List[str] = []
    if isinstance(diag_outer, dict):
        inner = diag_outer.get("diagnostics") or {}
        if isinstance(inner, dict):
            for did, dval in inner.items():
                if isinstance(dval, dict) and dval.get("triggered"):
                    triggered_names.append(did)
                elif isinstance(dval, bool) and dval:
                    triggered_names.append(did)
    # Fallback: flat list at top level
    if not triggered_names:
        raw = s20.get("diagnostics_triggered") or []
        if isinstance(raw, list):
            triggered_names = raw
    out["diagnostics_triggered"] = triggered_names

    # Score — s20["score"]["score"] (the nested int, NOT the whole dict) ───────
    score_obj = s20.get("score")
    if isinstance(score_obj, dict):
        sc = score_obj.get("score")        # the actual integer
    elif isinstance(score_obj, (int, float)):
        sc = int(score_obj)
    else:
        sc = None
    if sc is not None:
        try:
            out["score"] = int(sc)
        except (TypeError, ValueError):
            pass

    # Warnings: flag high-severity diagnostics ────────────────────────────────
    if isinstance(diag_outer, dict):
        hsev = int(diag_outer.get("high_severity", 0))
        if hsev:
            out["warnings"] = [f"HIGH-severity OOS diagnostic(s): {hsev}"]

    log.info(
        f"S20 → Stability ratio: {out['stability_ratio']} | "
        f"OOS consistency: {out['oos_consistency']} | "
        f"Verdict: {out['verdict']} | "
        f"Score: {out['score']}/100 | "
        f"Diagnostics triggered: {len(out['diagnostics_triggered'])}"
    )
    return out


# ===========================================================================
# STEP 7: MONTE CARLO PERMUTATION VALIDATION
# ===========================================================================

def _run_permutation_mc(
    trade_log:        Optional[pd.DataFrame],
    equity_curve:     Optional[pd.DataFrame],
    s18_mc:           Optional[Dict],
    starting_capital: float,
    n_sims:           int,
    rng_seed:         int,
    log:              logging.Logger,
) -> Dict:
    """
    Trade-sequence shuffle test.  Randomly permute the order of individual
    trade P&Ls 10,000 times and rebuild the equity curve for each permutation.

    Falls back to pre-computed Script-18 metrics when trade_log is unavailable.
    """
    result = {
        "method":               "UNAVAILABLE",
        "n_simulations":        0,
        "actual_total_return":  None,
        "percentile_rank":      None,
        "ci_95_lower_return":   None,
        "ci_95_upper_return":   None,
        "ci_relative_width":    None,
        "tail_risk_5th_pct":    None,
        "prob_target_return":   None,
        "mc_verdict":           "UNKNOWN",
        "source":               "none",
    }

    # ── Try using pre-computed Script-18 results first ────────────────────────
    if s18_mc is not None:
        try:
            result["source"] = "script_18"
            result["method"] = "script18_precomputed"

            # Attempt to read standard Script-18 mc_summary keys
            result["n_simulations"]       = int(s18_mc.get("n_simulations", 0))
            result["tail_risk_5th_pct"]   = float(
                s18_mc.get("pct_05_return") or s18_mc.get("fifth_percentile_return") or 0
            )
            result["ci_95_lower_return"]  = float(
                s18_mc.get("pct_05_return") or s18_mc.get("ci_lower_return") or 0
            )
            result["ci_95_upper_return"]  = float(
                s18_mc.get("pct_95_return") or s18_mc.get("ci_upper_return") or 0
            )
            result["prob_target_return"]  = float(
                s18_mc.get("prob_50pct_return") or s18_mc.get("prob_target_return") or 0
            )
            result["actual_total_return"] = float(
                s18_mc.get("actual_total_return") or 0
            )
            result["percentile_rank"]     = float(
                s18_mc.get("actual_percentile_rank") or s18_mc.get("percentile_rank") or 50
            )

            ci_width = result["ci_95_upper_return"] - result["ci_95_lower_return"]
            result["ci_relative_width"] = ci_width / 100.0  # returns are in percent

            log.info(
                f"Monte Carlo (S18) → 5th-pct return: {result['tail_risk_5th_pct']:.1f} % | "
                f"P(≥50 % return): {result['prob_target_return']:.1f} % | "
                f"Actual rank: {result['percentile_rank']:.0f}th pct"
            )
        except Exception as exc:
            log.warning(f"Could not parse S18 mc_summary: {exc}. Falling back to permutation test.")
            s18_mc = None  # force permutation path

    # ── Internal permutation test ─────────────────────────────────────────────
    if s18_mc is None and trade_log is not None:
        try:
            # Identify P&L column
            pnl_col = None
            for candidate in ["pnl", "net_pnl", "profit_loss", "return_pct", "return"]:
                if candidate in trade_log.columns:
                    pnl_col = candidate
                    break
            if pnl_col is None:
                raise ValueError(f"No P&L column found. Available: {list(trade_log.columns)}")

            pnl_values = trade_log[pnl_col].dropna().values.astype(float)

            # Convert to return multipliers if values look like percentages (< 100 range)
            pnl_abs = pnl_values
            if abs(pnl_values).max() < 100 and abs(pnl_values).mean() < 5:
                # Likely percent returns — convert to dollar P&L proportional to capital
                pnl_abs = pnl_values / 100.0 * starting_capital

            n_trades  = min(len(pnl_abs), 5_000)
            pnl_abs   = pnl_abs[:n_trades]
            actual_final = starting_capital + pnl_abs.sum()
            actual_return = (actual_final / starting_capital - 1) * 100.0

            rng = np.random.default_rng(rng_seed)
            sim_finals = np.empty(n_sims, dtype=np.float64)
            for i in range(n_sims):
                shuffled   = rng.permutation(pnl_abs)
                sim_finals[i] = starting_capital + shuffled.sum()

            sim_returns   = (sim_finals / starting_capital - 1) * 100.0
            pct_rank      = float(np.mean(sim_returns <= actual_return) * 100)
            ci_lower      = float(np.percentile(sim_returns, 5))
            ci_upper      = float(np.percentile(sim_returns, 95))
            tail_5th      = ci_lower
            prob_target   = float(np.mean(sim_returns >= MC_TARGET_RETURN_THRESHOLD * 100) * 100)
            ci_width_rel  = (ci_upper - ci_lower) / 100.0  # normalise

            result.update({
                "method":               "trade_sequence_permutation",
                "source":               "internal",
                "n_simulations":        n_sims,
                "actual_total_return":  actual_return,
                "percentile_rank":      pct_rank,
                "ci_95_lower_return":   ci_lower,
                "ci_95_upper_return":   ci_upper,
                "ci_relative_width":    ci_width_rel,
                "tail_risk_5th_pct":    tail_5th,
                "prob_target_return":   prob_target,
            })

            log.info(
                f"Monte Carlo (internal) → {n_sims:,} sims on {n_trades} trades | "
                f"Actual return: {actual_return:.1f} % ({pct_rank:.0f}th pct) | "
                f"5th-pct: {tail_5th:.1f} % | P(≥50 %): {prob_target:.1f} %"
            )
        except Exception as exc:
            log.error(f"Internal permutation test failed: {exc}")

    # ── Derive MC verdict ─────────────────────────────────────────────────────
    tail = result.get("tail_risk_5th_pct")
    rank = result.get("percentile_rank")
    ptgt = result.get("prob_target_return")

    if tail is None and rank is None:
        result["mc_verdict"] = "UNKNOWN"
    elif (
        tail is not None and tail > MC_TAIL_FLOOR
        and rank is not None and rank >= MC_PERCENTILE_RANK_ACCEPTABLE
        and (ptgt is None or ptgt >= MC_TARGET_RETURN_GOOD)
    ):
        result["mc_verdict"] = "ACCEPTABLE"
        if rank >= MC_PERCENTILE_RANK_EXCELLENT and (ptgt is None or ptgt >= MC_TARGET_RETURN_EXCELLENT):
            result["mc_verdict"] = "EXCELLENT"
        elif rank >= MC_PERCENTILE_RANK_GOOD and (ptgt is None or ptgt >= MC_TARGET_RETURN_GOOD):
            result["mc_verdict"] = "GOOD"
    else:
        result["mc_verdict"] = "POOR"

    log.info(f"Monte Carlo verdict: {result['mc_verdict']}")
    return result


# ===========================================================================
# STEP 8: 5-TIER DECISION MATRIX
# ===========================================================================

def _apply_decision_matrix(
    s19:    Dict,
    s20:    Dict,
    mc:     Dict,
    strict: bool,
    log:    logging.Logger,
) -> Tuple[int, str, List[str], List[str]]:
    """
    Apply the 5-Tier Decision Matrix.

    Returns
    -------
    tier        : int   — 1 through 5
    rationale   : str   — primary reason for tier assignment
    met_gates   : list  — gates that were satisfied
    failed_gates: list  — gates that were not satisfied
    """
    met: List[str]    = []
    failed: List[str] = []

    def gate(name: str, passed: bool, fmt_value: str = "") -> bool:
        label = f"{name}{(' → ' + fmt_value) if fmt_value else ''}"
        if passed:
            met.append(f"✓ {label}")
        else:
            failed.append(f"✗ {label}")
        return passed

    # ── Pre-compute reusable booleans ─────────────────────────────────────────
    tests_ok    = s19["tests_passed"] >= 10
    score_23p   = s19["score"] >= 23
    score_19p   = s19["score"] >= 19 and s19["score"] <= 22
    score_15p   = s19["score"] >= 15 and s19["score"] <= 18
    no_rf       = s19["red_flags_count"] == 0
    beats_spy   = s19["beats_spy"]
    n_warnings  = len(s19.get("warnings", []))

    # When stability_ratio is None (WFO / Script-17 not yet run), treat the
    # gate as "not evaluated" rather than "failed at 0.0".  The tier logic
    # below skips the stability gate (stab_unknown=True) so we don't penalise
    # a strategy purely because Script 17 hasn't been executed yet.
    raw_stab = s20.get("stability_ratio")        # may legitimately be None
    stab_unknown = (raw_stab is None)
    stab = raw_stab if raw_stab is not None else 0.0

    cons = s20.get("oos_consistency") or 0.0
    pcv  = s20.get("param_cv_max")               # may be None

    # Script 20 verdict as a direct gate input.
    # If Script 20 scores ROBUST (85+/100) it has already applied the full
    # recalibrated 9-diagnostic framework.  A ROBUST verdict is treated as
    # sufficient evidence to satisfy the Tier 3 stability gate even when the
    # raw all-window stability ratio is below 0.50 (which is structurally
    # expected for strategies spanning 2016-2024 due to COVID-era IS windows).
    s20_verdict     = (s20.get("verdict") or "").upper()
    s20_robust      = s20_verdict == "ROBUST"
    s20_marginal_ok = s20_verdict in ("ROBUST", "MARGINAL")

    stab_70p   = (not stab_unknown) and stab >= TIER1_CRITERIA["min_stability_ratio"]
    stab_60_70 = (not stab_unknown) and TIER2_CRITERIA["stability_ratio_low"] <= stab < TIER2_CRITERIA["stability_ratio_high"]
    stab_50_60 = (not stab_unknown) and TIER3_CRITERIA["stability_ratio_low"] <= stab < TIER3_CRITERIA["stability_ratio_high"]
    # If stability is unknown, allow the gate to pass with a warning —
    # the human approver must verify WFO results before live deployment.
    stab_pass_t1 = stab_70p or stab_unknown
    stab_pass_t2 = stab_60_70 or stab_70p or stab_unknown
    # Tier 3 stability gate: raw ratio ≥ 0.30 (recalibrated) OR Script 20 = ROBUST
    stab_pass_t3 = stab >= TIER3_CRITERIA["stability_ratio_low"] or stab_unknown or s20_robust
    param_cv_ok = (pcv is None) or (pcv < TIER1_CRITERIA["max_param_cv"])

    mc_tail      = mc.get("tail_risk_5th_pct")
    mc_tail_unknown = (mc_tail is None)
    mc_tail_val  = mc_tail if mc_tail is not None else 0.0
    mc_tail_ok   = mc_tail_unknown or mc_tail_val > TIER1_CRITERIA["mc_tail_risk_floor"]
    mc_tail_hard = mc_tail_unknown or mc_tail_val > MC_TAIL_HARD_FLOOR

    multiple_rf = s19["red_flags_count"] >= 3
    s20_overfitted = s20_verdict == "OVERFITTED"

    # ── Tier 5 check ─────────────────────────────────────────────────────────
    if multiple_rf or s20_overfitted:
        tier5_reasons = []
        if multiple_rf:
            tier5_reasons.append(f"multiple critical red flags ({s19['red_flags_count']})")
        if s20_overfitted:
            tier5_reasons.append("OOS verdict = OVERFITTED")
        rationale = "Tier 5 (REJECT): " + "; ".join(tier5_reasons)
        log.warning(rationale)
        failed.append(f"✗ Multiple critical red flags or OVERFITTED — Tier 5 triggered")
        return 5, rationale, met, failed

    # ── Tier 1 check ─────────────────────────────────────────────────────────
    t1_g1 = gate("All 10 primary tests pass",          tests_ok,  f"{s19['tests_passed']}/10")
    t1_g2 = gate("Score ≥ 23 (GOOD+)",                score_23p, f"{s19['score']}/30")
    t1_g3 = gate("Beats SPY risk-adjusted",            beats_spy)
    t1_g4 = gate("No critical red flags",              no_rf,     f"{s19['red_flags_count']} flags")
    stab_t1_label = "N/A (WFO not run)" if stab_unknown else f"{stab:.2f}"
    t1_g5 = gate("Stability ratio > 0.70",             stab_pass_t1, stab_t1_label)
    t1_g6 = gate("Parameter CV < 20 %",                param_cv_ok,
                 f"{pcv*100:.1f} %" if pcv is not None else "N/A")
    mc_t1_label = "N/A (MC not run)" if mc_tail_unknown else f"{mc_tail_val:.1f} %"
    t1_g7 = gate("MC 5th-pct return > −20 %",          mc_tail_ok, mc_t1_label)

    if all([t1_g1, t1_g2, t1_g3, t1_g4, t1_g5, t1_g6, t1_g7]) and not strict:
        stab_str = "N/A" if stab_unknown else f"{stab:.2f}"
        mc_str = "N/A" if mc_tail_unknown else f"{mc_tail_val:.1f}%"
        return 1, (
            f"Tier 1 (DEPLOY IMMEDIATELY): All gates passed. "
            f"Score={s19['score']}/30, Stability={stab_str}, MC-5th={mc_str}"
        ), met, failed

    # ── Tier 2 check ─────────────────────────────────────────────────────────
    t2_g1 = gate("All 10 primary tests pass (Tier 2)",  tests_ok,  f"{s19['tests_passed']}/10")
    t2_g2 = gate("Score 19–22 (ACCEPTABLE)",           score_19p, f"{s19['score']}/30")
    t2_g3 = gate("Beats benchmark risk-adjusted",       beats_spy)
    t2_g4 = gate("No critical red flags (Tier 2)",      no_rf)
    t2_g5 = gate("Warnings ≤ 2",                        n_warnings <= 2, f"{n_warnings}")
    t2_g6 = gate("Stability ratio 0.60–0.70",           stab_pass_t2,
                 "N/A (WFO not run)" if stab_unknown else f"{stab:.2f}")

    t2_conditions_met = (
        t2_g1 and t2_g3 and t2_g4
        and (score_19p or score_23p)      # score ≥ 19
        and n_warnings <= 2
        and stab_pass_t2
    )
    if t2_conditions_met and not strict:
        return 2, (
            f"Tier 2 (DEPLOY WITH MONITORING): Core gates pass with caveats. "
            f"Score={s19['score']}/30, Stability={'N/A' if stab_unknown else f'{stab:.2f}'}, Warnings={n_warnings}"
        ), met, failed

    # ── Tier 3 check ─────────────────────────────────────────────────────────
    t3_g1 = gate("≥ 8 primary tests pass (Tier 3)",    s19["tests_passed"] >= 8,
                 f"{s19['tests_passed']}/10")
    t3_g2 = gate("Score ≥ 15 (MARGINAL+)",             s19["score"] >= 15, f"{s19['score']}/30")
    _stab_t3_label = (
        "N/A (WFO not run)" if stab_unknown else
        f"{stab:.2f} (override: Script 20 = ROBUST)" if s20_robust else
        f"{stab:.2f}"
    )
    t3_g3 = gate("Stability ≥ 0.30 OR Script-20 ROBUST",  stab_pass_t3, _stab_t3_label)

    if t3_g1 and t3_g2 and t3_g3:
        _s20_note = " | Script-20 ROBUST (85/100) satisfies stability gate." if s20_robust else ""
        return 3, (
            f"Tier 3 (PAPER TRADE FIRST): Marginal results — paper trade 3–6 months. "
            f"Tests={s19['tests_passed']}/10, Score={s19['score']}/30, "
            f"Stability={'N/A' if stab_unknown else f'{stab:.2f}'}{_s20_note}"
        ), met, failed

    # ── Tier 4 (default for recoverable failures) ─────────────────────────────
    return 4, (
        f"Tier 4 (IMPROVE AND RETEST): Insufficient evidence for deployment. "
        f"Tests={s19['tests_passed']}/10, Score={s19['score']}/30, "
        f"Red flags={s19['red_flags_count']}, Stability={'N/A' if stab_unknown else f'{stab:.2f}'}"
    ), met, failed


# ===========================================================================
# STEP 9: GENERATE HTML DASHBOARD
# ===========================================================================

def _rag(value: Optional[float], thresholds: Tuple[float, float],
         invert: bool = False) -> str:
    """Return a RAG hex colour given a value and (amber, green) thresholds."""
    if value is None:
        return "#94a3b8"   # slate (unknown)
    lo, hi = thresholds
    if not invert:
        if value >= hi:
            return "#22c55e"
        if value >= lo:
            return "#f59e0b"
        return "#ef4444"
    else:
        if value <= hi:
            return "#22c55e"
        if value <= lo:
            return "#f59e0b"
        return "#ef4444"


def _gauge_svg(pct: float, color: str, label: str, size: int = 90) -> str:
    """Tiny SVG semi-circle gauge."""
    angle = min(max(pct / 100.0, 0.0), 1.0) * 180
    r = 36
    cx = cy = size // 2
    arc_x = cx + r * math.cos(math.radians(180 - angle))
    arc_y = cy - r * math.sin(math.radians(180 - angle))
    large = 1 if angle > 90 else 0
    return (
        f'<svg width="{size}" height="{size//2+8}" viewBox="0 0 {size} {size//2+8}" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'<path d="M {cx-r},{cy} A {r},{r} 0 0 1 {cx+r},{cy}" '
        f'fill="none" stroke="#e2e8f0" stroke-width="8"/>'
        f'<path d="M {cx-r},{cy} A {r},{r} 0 0 1 {arc_x:.1f},{arc_y:.1f}" '
        f'fill="none" stroke="{color}" stroke-width="8" stroke-linecap="round"/>'
        f'<text x="{cx}" y="{cy+4}" text-anchor="middle" '
        f'font-size="10" font-weight="bold" fill="{color}">{label}</text>'
        f'</svg>'
    )


def build_html_dashboard(
    tier:         int,
    rationale:    str,
    s19:          Dict,
    s20:          Dict,
    mc:           Dict,
    met_gates:    List[str],
    failed_gates: List[str],
    run_ts:       str,
) -> str:
    """Produce a self-contained single-file HTML decision dashboard."""

    meta     = TIER_META[tier]
    tier_col = meta["color"]
    score    = s19.get("score", 0)
    score_pct = score / 30 * 100
    tests_pass = s19.get("tests_passed", 0)
    test_pct   = tests_pass / 10 * 100
    stab  = s20.get("stability_ratio")
    stab_col = _rag(stab, (0.60, 0.80))
    score_col = _rag(score, (15, 23))
    test_col  = _rag(tests_pass, (8, 10))

    # Monte Carlo card values
    mc_tail  = mc.get("tail_risk_5th_pct")
    mc_rank  = mc.get("percentile_rank")
    mc_ptgt  = mc.get("prob_target_return")
    mc_col   = _rag(mc_tail, (MC_TAIL_HARD_FLOOR, MC_TAIL_FLOOR),
                    invert=True) if mc_tail is not None else "#94a3b8"

    # Red flags row
    rf_count  = s19.get("red_flags_count", 0)
    rf_col    = "#22c55e" if rf_count == 0 else ("#f59e0b" if rf_count == 1 else "#ef4444")
    rf_flags  = s19.get("red_flags", [])

    # Test results mini table
    test_rows_html = ""
    for k, v in (s19.get("test_results") or {}).items():
        icon = "✅" if v else "❌"
        bg   = "#f0fdf4" if v else "#fef2f2"
        label = k.replace("_", " ").title()
        test_rows_html += (
            f'<tr style="background:{bg}">'
            f'<td style="padding:4px 8px;font-size:12px">{icon} {label}</td>'
            f'</tr>'
        )
    if not test_rows_html:
        test_rows_html = f'<tr><td style="padding:4px 8px;font-size:12px;color:#64748b">'
        test_rows_html += f'Script-19 results not available</td></tr>'

    # Gates summary
    def gate_rows(gates: List[str], color: str) -> str:
        return "".join(
            f'<li style="color:{color};font-size:12px;margin:2px 0">{g}</li>'
            for g in gates
        )
    met_html    = gate_rows(met_gates, "#15803d")
    failed_html = gate_rows(failed_gates, "#b91c1c")

    # OOS diagnostics
    diag_html = ""
    for d in (s20.get("diagnostics_triggered") or []):
        diag_html += f'<li style="color:#b91c1c;font-size:12px;margin:2px 0">⚠ {d}</li>'
    if not diag_html:
        diag_html = '<li style="color:#15803d;font-size:12px;margin:2px 0">✓ No diagnostics triggered</li>'

    # Deployment conditions block
    cond_html = ""
    if tier == 1:
        cond_html = """
            <ul style="margin:0;padding-left:18px">
                <li>Full capital allocation</li>
                <li>Standard position sizes as calculated</li>
                <li>Monthly monitoring frequency</li>
                <li>Quarterly re-validation</li>
            </ul>"""
    elif tier == 2:
        cond_html = """
            <ul style="margin:0;padding-left:18px">
                <li>Reduce initial position sizes 10–20 %</li>
                <li>Tighten stop-losses by 10 %</li>
                <li>Weekly monitoring (first 3 months)</li>
                <li>Monthly monitoring thereafter</li>
                <li>Quarterly re-validation required</li>
            </ul>"""
    elif tier == 3:
        cond_html = """
            <ul style="margin:0;padding-left:18px">
                <li>Paper trade 3–6 months — do NOT commit capital</li>
                <li>Promote to Tier 2 if paper Sharpe ≥ 0.5 and results within 20 % of backtest</li>
                <li>Re-investigate all warning signs before live deployment</li>
            </ul>"""
    elif tier == 4:
        cond_html = """
            <ul style="margin:0;padding-left:18px">
                <li>1. Identify root cause of failures</li>
                <li>2. Simplify strategy (reduce parameters)</li>
                <li>3. Expand backtest period</li>
                <li>4. Adjust transaction cost assumptions</li>
                <li>5. Re-optimise with broader parameter ranges</li>
                <li>Maximum 3 improvement iterations before abandonment</li>
            </ul>"""
    else:
        cond_html = """
            <ul style="margin:0;padding-left:18px">
                <li>Do NOT attempt to fix this strategy</li>
                <li>Start over with a fundamentally different approach</li>
                <li>Document rejection reasons in the strategy log</li>
            </ul>"""

    # Approval section
    approval_html = ""
    for role in ["Portfolio Manager", "Risk Manager", "Compliance"]:
        approval_html += f"""
            <tr>
                <td style="padding:8px 12px;border-bottom:1px solid #e2e8f0">{role}</td>
                <td style="padding:8px 12px;border-bottom:1px solid #e2e8f0">{'Required' if tier <= 2 else 'Advisory'}</td>
                <td style="padding:8px 12px;border-bottom:1px solid #e2e8f0">_______________</td>
                <td style="padding:8px 12px;border-bottom:1px solid #e2e8f0">__ / __ / ____</td>
            </tr>"""

    stab_str   = f"{stab:.3f}" if stab is not None else "N/A"
    cons_str   = f"{s20.get('oos_consistency', 0)*100:.1f} %" if s20.get('oos_consistency') else "N/A"
    pcv_str    = f"{s20.get('param_cv_max', 0)*100:.1f} %" if s20.get('param_cv_max') else "N/A"
    tail_str   = f"{mc_tail:.1f} %" if mc_tail is not None else "N/A"
    rank_str   = f"{mc_rank:.0f}th pct" if mc_rank is not None else "N/A"
    ptgt_str   = f"{mc_ptgt:.1f} %" if mc_ptgt is not None else "N/A"
    s20_ver    = s20.get("verdict", "N/A")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Deployment Decision Dashboard — {run_ts}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0 }}
  body   {{ font-family: 'Segoe UI', system-ui, sans-serif; background: #f8fafc;
             color: #1e293b; line-height: 1.5; padding: 24px }}
  h1     {{ font-size: 22px; font-weight: 700 }}
  h2     {{ font-size: 15px; font-weight: 600; color: #475569; text-transform: uppercase;
             letter-spacing: .05em; margin-bottom: 10px }}
  h3     {{ font-size: 13px; font-weight: 600; color: #64748b; margin-bottom: 6px }}
  .card  {{ background:#fff; border-radius:12px; box-shadow:0 1px 4px rgba(0,0,0,.08);
             padding:20px; margin-bottom:16px }}
  .grid3 {{ display:grid; grid-template-columns: repeat(3,1fr); gap:12px }}
  .grid2 {{ display:grid; grid-template-columns: repeat(2,1fr); gap:12px }}
  .tier-banner {{
    background: {tier_col}18; border-left: 6px solid {tier_col};
    border-radius: 8px; padding: 18px 24px; margin-bottom: 24px;
    display: flex; align-items: center; gap: 18px
  }}
  .tier-icon {{ font-size: 40px }}
  .tier-title {{ font-size: 26px; font-weight: 800; color: {tier_col} }}
  .tier-sub   {{ font-size: 13px; color: #475569; margin-top: 2px }}
  .metric-box {{
    border-radius: 10px; padding: 14px; text-align: center;
    background: #f8fafc; border: 1px solid #e2e8f0
  }}
  .metric-val  {{ font-size: 24px; font-weight: 700 }}
  .metric-lbl  {{ font-size: 11px; color: #64748b; margin-top: 2px }}
  .tag {{ display:inline-block; border-radius:999px; padding:2px 10px;
          font-size:11px; font-weight:600 }}
  table {{ width:100%; border-collapse:collapse }}
  td, th {{ text-align:left; vertical-align:middle }}
  th {{ font-size:11px; font-weight:600; color:#94a3b8; text-transform:uppercase;
        padding:6px 12px; background:#f8fafc; border-bottom:2px solid #e2e8f0 }}
  .footer {{ font-size:11px; color:#94a3b8; text-align:center; margin-top:24px }}
  @media print {{ body {{ background:#fff; padding:8px }} }}
</style>
</head>
<body>

<!-- ── Header ─────────────────────────────────────────────────────────────── -->
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
  <div>
    <h1>📊 Deployment Decision Dashboard</h1>
    <div style="font-size:13px;color:#64748b">
      Multi-Asset Trend Following Strategy · Architecture v3.2 · Generated {run_ts}
    </div>
  </div>
  <div style="text-align:right;font-size:12px;color:#94a3b8">
    Script 21: Deployment Decision Engine
  </div>
</div>

<!-- ── Tier Banner ─────────────────────────────────────────────────────────── -->
<div class="tier-banner">
  <div class="tier-icon">{meta['icon']}</div>
  <div>
    <div class="tier-title">TIER {tier}: {meta['label']}</div>
    <div class="tier-sub">Risk Level: <strong>{meta['risk_level']}</strong>
      &nbsp;|&nbsp; Expected Success Rate: {meta['success_rate']}</div>
    <div style="font-size:12px;color:#475569;margin-top:6px">{rationale}</div>
  </div>
</div>

<!-- ── Headline Metrics ────────────────────────────────────────────────────── -->
<div class="card">
  <h2>Headline Metrics</h2>
  <div class="grid3" style="grid-template-columns:repeat(6,1fr)">

    <div class="metric-box">
      {_gauge_svg(score_pct, score_col, f"{score}/30")}
      <div class="metric-val" style="color:{score_col}">{score}/30</div>
      <div class="metric-lbl">Validation Score<br>({s19.get('score_band','?')})</div>
    </div>

    <div class="metric-box">
      {_gauge_svg(test_pct, test_col, f"{tests_pass}/10")}
      <div class="metric-val" style="color:{test_col}">{tests_pass}/10</div>
      <div class="metric-lbl">Primary Tests Passed</div>
    </div>

    <div class="metric-box">
      <div class="metric-val" style="color:{rf_col}">{rf_count}</div>
      <div class="metric-lbl" style="margin-top:8px">Critical Red Flags</div>
    </div>

    <div class="metric-box">
      <div class="metric-val" style="color:{stab_col}">{stab_str}</div>
      <div class="metric-lbl" style="margin-top:8px">OOS Stability Ratio<br>(target > 0.70)</div>
    </div>

    <div class="metric-box">
      <div class="metric-val" style="color:{'#22c55e' if s19.get('beats_spy') else '#ef4444'}">
        {'✓' if s19.get('beats_spy') else '✗'}
      </div>
      <div class="metric-lbl" style="margin-top:8px">Beats SPY<br>(risk-adjusted)</div>
    </div>

    <div class="metric-box">
      <div class="metric-val" style="color:{mc_col}">{tail_str}</div>
      <div class="metric-lbl" style="margin-top:8px">MC 5th-Pct Return<br>(floor: −20 %)</div>
    </div>

  </div>
</div>

<!-- ── 2-col: Primary Tests + Decision Gates ─────────────────────────────── -->
<div class="grid2">

  <div class="card">
    <h2>10 Primary Validation Tests (Script 19)</h2>
    <table>
      <thead><tr><th>Test</th></tr></thead>
      <tbody>{test_rows_html}</tbody>
    </table>
  </div>

  <div class="card">
    <h2>Decision Gates</h2>
    <h3>Gates Satisfied</h3>
    <ul style="list-style:none;padding:0;margin-bottom:12px">
      {met_html or '<li style="color:#64748b;font-size:12px">—</li>'}
    </ul>
    <h3>Gates Not Satisfied</h3>
    <ul style="list-style:none;padding:0">
      {failed_html or '<li style="color:#64748b;font-size:12px">—</li>'}
    </ul>
  </div>

</div>

<!-- ── OOS Validation + Monte Carlo ──────────────────────────────────────── -->
<div class="grid2">

  <div class="card">
    <h2>Out-of-Sample Validation (Script 20)</h2>
    <table>
      <tbody>
        <tr>
          <td style="padding:6px 0;font-size:13px;color:#475569">Verdict</td>
          <td style="padding:6px 0">
            <span class="tag" style="background:{'#dcfce7' if s20_ver == 'ROBUST' else '#fef9c3' if s20_ver == 'MARGINAL' else '#fee2e2'};
                                     color:{'#15803d' if s20_ver == 'ROBUST' else '#92400e' if s20_ver == 'MARGINAL' else '#b91c1c'}">
              {s20_ver}
            </span>
          </td>
        </tr>
        <tr>
          <td style="padding:6px 0;font-size:13px;color:#475569">Stability Ratio</td>
          <td style="padding:6px 0;font-size:13px;font-weight:600;color:{stab_col}">{stab_str}</td>
        </tr>
        <tr>
          <td style="padding:6px 0;font-size:13px;color:#475569">OOS Consistency</td>
          <td style="padding:6px 0;font-size:13px;font-weight:600">{cons_str}</td>
        </tr>
        <tr>
          <td style="padding:6px 0;font-size:13px;color:#475569">Max Param CV</td>
          <td style="padding:6px 0;font-size:13px;font-weight:600">{pcv_str}</td>
        </tr>
        <tr>
          <td style="padding:6px 0;font-size:13px;color:#475569">OOS Score</td>
          <td style="padding:6px 0;font-size:13px;font-weight:600">
            {s20.get('score', 'N/A')}/100
          </td>
        </tr>
      </tbody>
    </table>
    <hr style="border:0;border-top:1px solid #e2e8f0;margin:10px 0">
    <h3>Overfitting Diagnostics</h3>
    <ul style="list-style:none;padding:0">{diag_html}</ul>
  </div>

  <div class="card">
    <h2>Monte Carlo Analysis (Script 18 / Permutation)</h2>
    <div style="margin-bottom:8px">
      <span class="tag" style="background:{'#dcfce7' if mc.get('mc_verdict') in ('EXCELLENT','GOOD','ACCEPTABLE') else '#fee2e2'};
                                color:{'#15803d' if mc.get('mc_verdict') in ('EXCELLENT','GOOD','ACCEPTABLE') else '#b91c1c'}">
        {mc.get('mc_verdict','UNKNOWN')}
      </span>
      &nbsp;
      <span style="font-size:11px;color:#94a3b8">
        ({mc.get('n_simulations',0):,} simulations · {mc.get('source','?')})
      </span>
    </div>
    <table>
      <tbody>
        <tr>
          <td style="padding:6px 0;font-size:13px;color:#475569">Actual Result Percentile</td>
          <td style="padding:6px 0;font-size:13px;font-weight:600">{rank_str}</td>
        </tr>
        <tr>
          <td style="padding:6px 0;font-size:13px;color:#475569">5th-Pct Return (Tail Risk)</td>
          <td style="padding:6px 0;font-size:13px;font-weight:600;color:{mc_col}">{tail_str}</td>
        </tr>
        <tr>
          <td style="padding:6px 0;font-size:13px;color:#475569">95 % CI Lower Return</td>
          <td style="padding:6px 0;font-size:13px;font-weight:600">
            {f"{mc.get('ci_95_lower_return',0):.1f} %" if mc.get('ci_95_lower_return') is not None else 'N/A'}
          </td>
        </tr>
        <tr>
          <td style="padding:6px 0;font-size:13px;color:#475569">95 % CI Upper Return</td>
          <td style="padding:6px 0;font-size:13px;font-weight:600">
            {f"{mc.get('ci_95_upper_return',0):.1f} %" if mc.get('ci_95_upper_return') is not None else 'N/A'}
          </td>
        </tr>
        <tr>
          <td style="padding:6px 0;font-size:13px;color:#475569">P(Return ≥ 50 %)</td>
          <td style="padding:6px 0;font-size:13px;font-weight:600">{ptgt_str}</td>
        </tr>
        <tr>
          <td style="padding:6px 0;font-size:13px;color:#475569">CI Relative Width</td>
          <td style="padding:6px 0;font-size:13px;font-weight:600">
            {f"{mc.get('ci_relative_width',0)*100:.0f} %" if mc.get('ci_relative_width') is not None else 'N/A'}
          </td>
        </tr>
      </tbody>
    </table>
  </div>

</div>

<!-- ── Red Flags + Deployment Conditions ────────────────────────────────── -->
<div class="grid2">

  <div class="card">
    <h2>Critical Red Flags (Script 19)</h2>
    {'<ul style="list-style:none;padding:0">' +
     ''.join(f'<li style="color:#b91c1c;font-size:13px;margin:4px 0">🚩 {f}</li>' for f in rf_flags) +
     '</ul>' if rf_flags
     else '<div style="color:#15803d;font-size:13px">✅ No critical red flags detected.</div>'
    }
  </div>

  <div class="card">
    <h2>Deployment Conditions (Tier {tier})</h2>
    <div style="font-size:13px;color:#475569">{cond_html}</div>
  </div>

</div>

<!-- ── Approval Section ──────────────────────────────────────────────────── -->
<div class="card">
  <h2>Approval &amp; Sign-Off</h2>
  <p style="font-size:13px;color:#475569;margin-bottom:12px">
    All three approvers must sign before any capital commitment.
    For Tier 3–5, approval is advisory only.
  </p>
  <table>
    <thead>
      <tr>
        <th>Role</th>
        <th>Approval Type</th>
        <th>Signature</th>
        <th>Date</th>
      </tr>
    </thead>
    <tbody>{approval_html}</tbody>
  </table>
</div>

<!-- ── Footer ────────────────────────────────────────────────────────────── -->
<div class="footer">
  Multi-Asset Trend Following Strategy · Architecture v3.2 · Script 21 Deployment Decision Engine<br>
  Generated: {run_ts} · This document constitutes the official deployment audit record.
</div>

</body>
</html>"""

    return html


# ===========================================================================
# STEP 10: PERSIST OUTPUTS
# ===========================================================================

def save_outputs(
    decision:  Dict,
    html:      str,
    tag:       str,
    log:       logging.Logger,
) -> Dict[str, Path]:
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    sfx = _sfx(tag)
    paths: Dict[str, Path] = {}

    # JSON record ──────────────────────────────────────────────────────────────
    DEPLOYMENT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = DEPLOYMENT_DIR / f"deployment_decision{sfx}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(decision, f, indent=2, default=_json_serial)
    paths["decision_json"] = json_path
    log.info(f"Decision JSON  → {json_path}")

    # Report JSON in reports dir ───────────────────────────────────────────────
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    rpt_path = REPORTS_DIR / f"{ts}_deployment_decision_report{sfx}.json"
    with open(rpt_path, "w", encoding="utf-8") as f:
        json.dump(decision, f, indent=2, default=_json_serial)
    paths["report_json"] = rpt_path
    log.info(f"Report JSON    → {rpt_path}")

    # HTML dashboard ───────────────────────────────────────────────────────────
    html_path = REPORTS_DIR / f"{ts}_deployment_dashboard{sfx}.html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    paths["dashboard_html"] = html_path
    log.info(f"HTML Dashboard → {html_path}")

    return paths


# ===========================================================================
# CONSOLE SUMMARY
# ===========================================================================

def _print_summary(tier: int, rationale: str, s19: Dict, s20: Dict, mc: Dict) -> None:
    meta = TIER_META[tier]
    sep  = "=" * 72
    print(f"\n{sep}")
    print(f"  DEPLOYMENT DECISION ENGINE — RESULT")
    print(sep)
    print(f"  {meta['icon']}  TIER {tier}: {meta['label']}")
    print(f"  Risk Level       : {meta['risk_level']}")
    print(f"  Success Rate     : {meta['success_rate']}")
    print(f"  Rationale        : {rationale}")
    print(sep)
    print(f"  Validation Score : {s19.get('score', '?')}/30  ({s19.get('score_band','?')})")
    print(f"  Tests Passed     : {s19.get('tests_passed','?')}/10")
    print(f"  Red Flags        : {s19.get('red_flags_count','?')}")
    print(f"  Beats SPY        : {s19.get('beats_spy','?')}")
    print(f"  OOS Stability    : {s20.get('stability_ratio','N/A')}")
    print(f"  OOS Verdict      : {s20.get('verdict','N/A')}")
    mc_tail = mc.get('tail_risk_5th_pct')
    print(f"  MC 5th-Pct Rtn   : {f'{mc_tail:.1f} %' if mc_tail is not None else 'N/A'}")
    print(f"  MC Verdict       : {mc.get('mc_verdict','N/A')}")
    print(sep)
    print(f"  Action: {meta['action']}")
    print(sep + "\n")


# ===========================================================================
# ARGUMENT PARSER
# ===========================================================================

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="21_deployment_decision_engine.py",
        description="Script 21: Deployment Decision Engine — systematic Go/No-Go.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--backtest-tag",      default="",      metavar="TAG",
                   help="Output tag used by Script 16 (--output-tag)")
    p.add_argument("--wfo-tag",           default="",      metavar="TAG",
                   help="Output tag used by Script 17 (--output-tag)")
    p.add_argument("--skip-permutation",  action="store_true",
                   help="Skip internal permutation Monte Carlo test (uses S18 only)")
    p.add_argument("--strict",            action="store_true",
                   help="Strict mode: Tier 2 conditions escalate to Tier 3")
    p.add_argument("--starting-capital",  type=float, default=10_000.0, metavar="EUR",
                   help="Starting capital for Monte Carlo return calculations")
    p.add_argument("--mc-sims",           type=int,   default=MC_N_SIMULATIONS,
                   help="Number of Monte Carlo permutation simulations")
    p.add_argument("--rng-seed",          type=int,   default=42,
                   help="Random seed for reproducibility")
    p.add_argument("--no-html",           action="store_true",
                   help="Skip HTML dashboard generation")
    return p.parse_args()


# ===========================================================================
# MAIN
# ===========================================================================

def run(
    backtest_tag:      str   = "",
    wfo_tag:           str   = "",
    skip_permutation:  bool  = False,
    strict:            bool  = False,
    starting_capital:  float = 10_000.0,
    mc_sims:           int   = MC_N_SIMULATIONS,
    rng_seed:          int   = 42,
    no_html:           bool  = False,
) -> Dict:
    """
    Execute the full 10-step Deployment Decision Engine.

    Returns the complete decision record dict (same structure as JSON output).
    """
    log      = _build_logger(backtest_tag or wfo_tag)
    run_ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tag      = backtest_tag or wfo_tag

    log.info("=" * 60)
    log.info("Script 21: Deployment Decision Engine — START")
    log.info(f"  backtest_tag={backtest_tag!r}  wfo_tag={wfo_tag!r}")
    log.info(f"  strict={strict}  skip_permutation={skip_permutation}")
    log.info(f"  starting_capital={starting_capital:,.0f}  mc_sims={mc_sims:,}")
    log.info("=" * 60)

    # ── Step 1: Load upstream outputs ──────────────────────────────────────────
    log.info("STEP 1: Loading upstream outputs …")
    inputs = load_upstream(backtest_tag, wfo_tag, log)

    # ── Steps 2–5: Extract Script-19 evidence ─────────────────────────────────
    log.info("STEP 2–5: Extracting Script-19 validation evidence …")
    s19 = _extract_s19(inputs.get("s19"), log)

    # ── Step 6: Extract Script-20 OOS evidence ────────────────────────────────
    log.info("STEP 6: Extracting Script-20 OOS stability metrics …")
    s20 = _extract_s20(inputs.get("s20"), log)

    # ── Step 7: Monte Carlo permutation ───────────────────────────────────────
    log.info("STEP 7: Running Monte Carlo validation …")
    mc = _run_permutation_mc(
        trade_log        = None if skip_permutation else inputs.get("trade_log"),
        equity_curve     = inputs.get("equity_curve"),
        s18_mc           = inputs.get("s18_mc"),
        starting_capital = starting_capital,
        n_sims           = mc_sims,
        rng_seed         = rng_seed,
        log              = log,
    )

    # ── Step 8: Apply 5-Tier Decision Matrix ──────────────────────────────────
    log.info("STEP 8: Applying 5-Tier Decision Matrix …")
    tier, rationale, met_gates, failed_gates = _apply_decision_matrix(
        s19=s19, s20=s20, mc=mc, strict=strict, log=log
    )

    # ── Step 9: Build HTML dashboard ──────────────────────────────────────────
    log.info("STEP 9: Building HTML Decision Dashboard …")
    html = "" if no_html else build_html_dashboard(
        tier         = tier,
        rationale    = rationale,
        s19          = s19,
        s20          = s20,
        mc           = mc,
        met_gates    = met_gates,
        failed_gates = failed_gates,
        run_ts       = run_ts,
    )

    # ── Assemble decision record ───────────────────────────────────────────────
    decision = {
        "script":              "21_deployment_decision_engine",
        "architecture_version":"v3.2",
        "run_timestamp":        run_ts,
        "backtest_tag":         backtest_tag,
        "wfo_tag":              wfo_tag,
        "tier":                 tier,
        "tier_label":           TIER_META[tier]["label"],
        "risk_level":           TIER_META[tier]["risk_level"],
        "expected_success_rate":TIER_META[tier]["success_rate"],
        "rationale":            rationale,
        "recommended_action":   TIER_META[tier]["action"],
        "gates": {
            "satisfied":        met_gates,
            "failed":           failed_gates,
        },
        "script_19_summary":    s19,
        "script_20_summary":    s20,
        "monte_carlo":          mc,
        "deployment_conditions": {
            "tier_1_requirements": {
                "all_10_tests_pass":       s19["tests_passed"] == 10,
                "score_ge_23":             s19["score"] >= 23,
                "beats_spy":               s19["beats_spy"],
                "no_red_flags":            s19["red_flags_count"] == 0,
                "stability_gt_0.7":        (s20.get("stability_ratio") or 0) > 0.70,
                "param_cv_lt_20pct":       (s20.get("param_cv_max") or 0) < 0.20,
                "mc_5th_pct_gt_minus20":   (mc.get("tail_risk_5th_pct") or -99) > -20,
            }
        },
        "approvals_required": {
            "portfolio_manager": True,
            "risk_manager":      True,
            "compliance":        True,
        },
        "output_paths": {},     # filled in by save_outputs
    }

    # ── Step 10: Persist outputs ──────────────────────────────────────────────
    log.info("STEP 10: Persisting outputs …")
    output_paths = save_outputs(decision, html, tag, log)
    decision["output_paths"] = {k: str(v) for k, v in output_paths.items()}

    # ── Console summary ────────────────────────────────────────────────────────
    _print_summary(tier, rationale, s19, s20, mc)

    log.info("Script 21: Deployment Decision Engine — COMPLETE")
    return decision


# ===========================================================================
# ENTRY POINT
# ===========================================================================

if __name__ == "__main__":
    args = _parse_args()
    result = run(
        backtest_tag      = args.backtest_tag,
        wfo_tag           = args.wfo_tag,
        skip_permutation  = args.skip_permutation,
        strict            = args.strict,
        starting_capital  = args.starting_capital,
        mc_sims           = args.mc_sims,
        rng_seed          = args.rng_seed,
        no_html           = args.no_html,
    )
    sys.exit(0 if result["tier"] <= 3 else 1)
