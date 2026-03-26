"""
config/strategies.py
====================
Strategy registry loader — Architecture v3.9 (Mar 2026)

Provides a single, consistent interface for resolving strategy names,
loading their configs, and building namespaced output paths. Every script
that supports --strategy imports from here. The registry itself lives in
config/strategies.json — add a strategy there; no script code changes needed.

Usage
-----
    from config.strategies import StrategyRegistry, resolve_strategies

    # Parse --strategy from CLI and return list of StrategyDef objects
    strategies = resolve_strategies(args.strategy, project_root=PROJECT_ROOT)

    for s in strategies:
        print(s.name)          # e.g. "sma_dist"
        print(s.label)         # e.g. "SMA Distance"
        print(s.config_path)   # absolute Path to strategy_parameters JSON
        print(s.color)         # e.g. "#27ae60"
        print(s.deployed)      # True = live capital, False = paper
        signals_dir = s.signals_dir(DATA_CACHE_DIR)   # data_cache/signals/sma_dist/
        reports_dir = s.reports_dir(PROJECT_ROOT, "rebalancing")  # reports/rebalancing/sma_dist/

Architecture: v3.9 (Mar 2026)
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


# ============================================================================
# DATA CLASS
# ============================================================================

@dataclass(frozen=True)
class StrategyDef:
    """
    Immutable descriptor for one strategy variant.

    Attributes
    ----------
    name        Short snake_case key (e.g. "sma_dist"). Used in directory
                names, CLI flags, and JSON output fields.
    label       Human-readable display name (e.g. "SMA Distance").
    config_path Absolute Path to the strategy_parameters JSON file.
    color       Hex color string for charts/dashboards.
    active      Whether to include in --strategy all runs.
    deployed    True = live capital. False = paper-trading only.
    description One-sentence summary of formula and key params.
    """
    name:        str
    label:       str
    config_path: Path
    color:       str
    active:      bool
    deployed:    bool
    description: str

    # ── Output path helpers ──────────────────────────────────────────────────

    def signals_dir(self, data_cache_dir: Path) -> Path:
        """data_cache/signals/{name}/"""
        return data_cache_dir / "signals" / self.name

    def portfolio_dir(self, data_cache_dir: Path) -> Path:
        """data_cache/portfolio/{name}/"""
        return data_cache_dir / "portfolio" / self.name

    def reports_dir(self, project_root: Path, report_type: str) -> Path:
        """reports/{report_type}/{name}/"""
        return project_root / "reports" / report_type / self.name

    def backtest_dir(self, data_cache_dir: Path) -> Path:
        """data_cache/backtest/{name}/"""
        return data_cache_dir / "backtest" / self.name

    def wfo_dir(self, data_cache_dir: Path) -> Path:
        """data_cache/backtest/{name}/walk_forward/"""
        return data_cache_dir / "backtest" / self.name / "walk_forward"

    def mc_dir(self, data_cache_dir: Path) -> Path:
        """data_cache/monte_carlo/{name}/"""
        return data_cache_dir / "monte_carlo" / self.name

    def deployment_dir(self, data_cache_dir: Path) -> Path:
        """data_cache/deployment/{name}/"""
        return data_cache_dir / "deployment" / self.name

    def status_badge(self) -> str:
        """Short label for report badges: LIVE or PAPER."""
        return "LIVE" if self.deployed else "PAPER"


# ============================================================================
# REGISTRY
# ============================================================================

class StrategyRegistry:
    """
    Loads and caches config/strategies.json. Resolves --strategy arguments.

    Parameters
    ----------
    project_root    Absolute path to the project root directory.
                    Defaults to the parent of this file's directory.
    registry_path   Override path to strategies.json. Defaults to
                    {project_root}/config/strategies.json.
    """

    _REGISTRY_FILENAME = "strategies.json"
    _SENTINEL_ALL      = "all"

    def __init__(
        self,
        project_root:   Optional[Path] = None,
        registry_path:  Optional[Path] = None,
    ) -> None:
        self._project_root = project_root or Path(__file__).resolve().parent.parent
        self._registry_path = (
            registry_path
            if registry_path is not None
            else self._project_root / "config" / self._REGISTRY_FILENAME
        )
        self._strategies: dict[str, StrategyDef] = {}
        self._load()

    # ── Private ──────────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Parse strategies.json and populate the internal dict."""
        if not self._registry_path.exists():
            raise FileNotFoundError(
                f"Strategy registry not found: {self._registry_path}\n"
                f"Expected at config/strategies.json under project root "
                f"({self._project_root}). Create it or check your working directory."
            )

        with open(self._registry_path, encoding="utf-8") as fh:
            raw = json.load(fh)

        entries = raw.get("strategies", {})
        if not entries:
            raise ValueError(
                f"strategies.json contains no strategy entries: {self._registry_path}"
            )

        for name, cfg in entries.items():
            # Resolve config path relative to project root
            cfg_path = Path(cfg["config"])
            if not cfg_path.is_absolute():
                cfg_path = self._project_root / cfg_path

            self._strategies[name] = StrategyDef(
                name        = name,
                label       = cfg["label"],
                config_path = cfg_path,
                color       = cfg.get("color", "#888888"),
                active      = bool(cfg.get("active", True)),
                deployed    = bool(cfg.get("deployed", False)),
                description = cfg.get("description", ""),
            )

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def all_strategies(self) -> List[StrategyDef]:
        """All registered strategies (active and inactive)."""
        return list(self._strategies.values())

    @property
    def active_strategies(self) -> List[StrategyDef]:
        """Only strategies marked active=true."""
        return [s for s in self._strategies.values() if s.active]

    def get(self, name: str) -> StrategyDef:
        """
        Return the StrategyDef for a given name. Raises KeyError if not found.
        """
        if name not in self._strategies:
            available = ", ".join(self._strategies.keys())
            raise KeyError(
                f"Unknown strategy '{name}'. "
                f"Available strategies: {available}. "
                f"Check config/strategies.json."
            )
        return self._strategies[name]

    def resolve(self, strategy_arg: Optional[str]) -> List[StrategyDef]:
        """
        Resolve a --strategy CLI value to a list of StrategyDef objects.

        Rules
        -----
        None or "all"  → all active strategies (default behaviour)
        "sma_dist"     → [StrategyDef(name="sma_dist", ...)]
        "sma_dist,roc" → [StrategyDef(sma_dist), StrategyDef(roc_weight)]
                         (comma-separated, no spaces)

        Raises ValueError for unknown names so callers fail loudly.
        """
        if strategy_arg is None or strategy_arg.strip().lower() == self._SENTINEL_ALL:
            return self.active_strategies

        names = [n.strip() for n in strategy_arg.split(",") if n.strip()]
        result: List[StrategyDef] = []
        for n in names:
            try:
                result.append(self.get(n))
            except KeyError as exc:
                raise ValueError(str(exc)) from exc
        return result

    def names(self) -> List[str]:
        """List of all registered strategy names."""
        return list(self._strategies.keys())

    def active_names(self) -> List[str]:
        """List of active strategy names."""
        return [s.name for s in self.active_strategies]

    def __repr__(self) -> str:
        return (
            f"StrategyRegistry({len(self._strategies)} strategies, "
            f"{len(self.active_strategies)} active, "
            f"registry={self._registry_path})"
        )


# ============================================================================
# MODULE-LEVEL CONVENIENCE FUNCTIONS
# ============================================================================

def resolve_strategies(
    strategy_arg:   Optional[str],
    project_root:   Optional[Path] = None,
    registry_path:  Optional[Path] = None,
) -> List[StrategyDef]:
    """
    One-call helper used by every script's main() function.

    Parameters
    ----------
    strategy_arg    Value of --strategy CLI argument (None = all active).
    project_root    Project root path. Auto-detected if None.
    registry_path   Override path to strategies.json. Auto-detected if None.

    Returns
    -------
    List of StrategyDef, always at least one element.

    Example
    -------
        strategies = resolve_strategies(args.strategy, project_root=PROJECT_ROOT)
        for s in strategies:
            run_for_strategy(s)
    """
    registry = StrategyRegistry(
        project_root  = project_root,
        registry_path = registry_path,
    )
    return registry.resolve(strategy_arg)


def add_strategy_argument(parser) -> None:
    """
    Add the standard --strategy argument to any argparse.ArgumentParser.

    Usage
    -----
        parser = argparse.ArgumentParser(...)
        add_strategy_argument(parser)
        args = parser.parse_args()
        strategies = resolve_strategies(args.strategy, PROJECT_ROOT)
    """
    parser.add_argument(
        "--strategy",
        metavar="NAME",
        default=None,
        help=(
            "Strategy to run. Use the name from config/strategies.json "
            "(e.g. 'sma_dist', 'roc_weight'). "
            "Comma-separate for multiple: --strategy sma_dist,roc_weight. "
            "Omit or pass 'all' to run all active strategies (default)."
        ),
    )
