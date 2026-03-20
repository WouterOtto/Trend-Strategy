#!/usr/bin/env python3
"""
reset_portfolio.py
==================
TrendFollowingOS — Portfolio Reset Utility
Implements Step 7 (Full Reset) and Step 8 (Selective Resets)
from the Operations Guide v3.3, 19 Mar 2026.

Usage
-----
  python reset_portfolio.py --full              # Step 7  — full portfolio reset
  python reset_portfolio.py --scenario A        # Step 8A — reset positions, keep ledger
  python reset_portfolio.py --scenario B        # Step 8B — clear logs only
  python reset_portfolio.py --scenario C        # Step 8C — clear stale reports
  python reset_portfolio.py --scenario D        # Step 8D — reset backtest / validation
  python reset_portfolio.py --scenario E        # Step 8E — nuclear rebuild

Optional flags
  --base-dir PATH     Override project root (default: ~/Desktop/Trade/trend_strategy_dev)
  --backup-dir PATH   Override backup destination (default: ~/Desktop)
  --no-backup         Skip backup step (NOT recommended)
  --yes               Skip all confirmation prompts (CI/scripted use only)
  --dry-run           Print what would be deleted without deleting anything
  --verify-only       Run the post-reset verification checks without deleting
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path

# ── ANSI colour helpers ────────────────────────────────────────────────────────

RED    = "\033[91m"
YELLOW = "\033[93m"
GREEN  = "\033[92m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def info(msg: str)    -> None: print(f"{CYAN}[INFO]{RESET}  {msg}")
def ok(msg: str)      -> None: print(f"{GREEN}[OK]{RESET}    {msg}")
def warn(msg: str)    -> None: print(f"{YELLOW}[WARN]{RESET}  {msg}")
def error(msg: str)   -> None: print(f"{RED}[ERROR]{RESET} {msg}", file=sys.stderr)
def header(msg: str)  -> None: print(f"\n{BOLD}{CYAN}{'─'*60}{RESET}\n{BOLD}{msg}{RESET}\n{'─'*60}")

# ── Path catalogue (mirrors Operations Guide §7.1 and §9) ─────────────────────

def build_paths(base: Path) -> dict:
    """Return every path referenced in the Operations Guide, keyed by label."""
    return {
        # Core portfolio state
        "portfolio_state":         base / "data" / "portfolio_state.json",
        "trade_ledger":            base / "data" / "trade_ledger.jsonl",
        "execution_template":      base / "data" / "execution_template.csv",
        "portfolio_state_backups": base / "data" / "portfolio_state_backups",
        # Reports
        "reports_executions":      base / "reports" / "executions",
        "reports_rebalancing":     base / "reports" / "rebalancing",
        "reports_daily":           base / "reports" / "daily",
        "reports_charts":          base / "reports" / "charts",
        "reports_backtest":        base / "reports" / "backtest",
        "reports_validation":      base / "reports" / "validation",
        "reports_oos_validation":  base / "reports" / "oos_validation",
        "reports_deployment":      base / "reports" / "deployment",
        # Logs
        "logs":                    base / "logs",
        # Data-cache (backtest / validation only — never the market data cache)
        "cache_backtest":          base / "data_cache" / "backtest",
        "cache_monte_carlo":       base / "data_cache" / "monte_carlo",
        "cache_validation":        base / "data_cache" / "validation",
        "cache_oos_validation":    base / "data_cache" / "oos_validation",
        "cache_deployment":        base / "data_cache" / "deployment",
        # Nuclear — entire top-level directories
        "data_dir":                base / "data",
        "data_cache_dir":          base / "data_cache",
        "reports_dir":             base / "reports",
        "logs_dir":                base / "logs",
    }

# ── Backup helpers ─────────────────────────────────────────────────────────────

def create_backup(base: Path, backup_dir: Path, tag: str, paths: dict,
                  keys: list[str], dry_run: bool = False) -> Path | None:
    """
    Create a timestamped .tar.gz archive of the specified path keys.
    Returns the archive path on success, None if nothing was found to archive.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_name = f"portfolio_{tag}_{timestamp}.tar.gz"
    archive_path = backup_dir / archive_name

    sources = [paths[k] for k in keys if paths[k].exists()]
    if not sources:
        warn("Nothing to back up — all target paths are already absent.")
        return None

    info(f"Creating backup → {archive_path}")
    if dry_run:
        for s in sources:
            info(f"  [DRY-RUN] would archive: {s}")
        return archive_path

    backup_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "w:gz") as tar:
        for src in sources:
            tar.add(src, arcname=src.relative_to(base.parent))

    size_mb = archive_path.stat().st_size / 1_048_576
    ok(f"Backup created — {archive_path} ({size_mb:.1f} MB)")
    return archive_path

# ── Cron-job helpers ───────────────────────────────────────────────────────────

def _get_crontab() -> list[str]:
    """Return current crontab lines, or [] if no crontab exists."""
    result = subprocess.run(["crontab", "-l"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()

def suspend_cron(dry_run: bool = False) -> list[str] | None:
    """
    Comment out all trend-strategy cron lines.
    Returns the original crontab lines so they can be restored later.
    """
    lines = _get_crontab()
    if not lines:
        warn("No crontab found — skipping cron suspension.")
        return None

    # Identify lines that reference this project
    keywords = ["trend_strategy", "00_trend_strategy", "cron_daily", "cron_weekly"]
    modified = []
    changed = False
    for line in lines:
        if any(kw in line for kw in keywords) and not line.strip().startswith("#"):
            modified.append(f"# [SUSPENDED] {line}")
            changed = True
        else:
            modified.append(line)

    if not changed:
        warn("No active trend-strategy cron entries found.")
        return lines

    info("Suspending trend-strategy cron jobs …")
    if dry_run:
        for m in modified:
            info(f"  [DRY-RUN] {m}")
        return lines

    new_crontab = "\n".join(modified) + "\n"
    proc = subprocess.run(["crontab", "-"], input=new_crontab,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        warn(f"Could not update crontab: {proc.stderr.strip()}")
    else:
        ok("Cron jobs suspended.")
    return lines

def restore_cron(original_lines: list[str], dry_run: bool = False) -> None:
    """Restore crontab to original state."""
    if original_lines is None:
        return
    info("Restoring cron jobs …")
    if dry_run:
        info("  [DRY-RUN] crontab would be restored.")
        return
    new_crontab = "\n".join(original_lines) + "\n"
    subprocess.run(["crontab", "-"], input=new_crontab,
                   capture_output=True, text=True)
    ok("Cron jobs restored.")

# ── Deletion helpers ───────────────────────────────────────────────────────────

def delete_path(p: Path, dry_run: bool = False) -> None:
    """Delete a file or directory tree, with dry-run support."""
    if not p.exists():
        info(f"  already absent: {p}")
        return
    if dry_run:
        kind = "dir" if p.is_dir() else "file"
        info(f"  [DRY-RUN] would delete ({kind}): {p}")
        return
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()
    ok(f"  deleted: {p}")

def glob_delete(directory: Path, pattern: str, dry_run: bool = False) -> None:
    """Delete all files matching a glob inside a directory."""
    if not directory.exists():
        return
    for f in directory.glob(pattern):
        delete_path(f, dry_run)

# ── Verification ───────────────────────────────────────────────────────────────

def verify_reset(base: Path, paths: dict, scenario: str = "full") -> bool:
    """
    Post-reset verification — mirrors the 'Step 6 — Verify the reset' section
    of the Operations Guide.
    """
    header("Verification")
    all_ok = True

    # Files that MUST be absent after a full reset
    must_be_gone = {
        "portfolio_state":         "data/portfolio_state.json",
        "trade_ledger":            "data/trade_ledger.jsonl",
        "execution_template":      "data/execution_template.csv",
    }
    # Directories that MUST be absent after a full reset
    must_be_gone_dirs = {
        "portfolio_state_backups": "data/portfolio_state_backups/",
        "reports_executions":      "reports/executions/",
        "reports_rebalancing":     "reports/rebalancing/",
        "reports_daily":           "reports/daily/",
    }

    if scenario == "full":
        for key, label in must_be_gone.items():
            if paths[key].exists():
                error(f"  STILL PRESENT: {label}")
                all_ok = False
            else:
                ok(f"  absent (expected): {label}")

        for key, label in must_be_gone_dirs.items():
            if paths[key].exists():
                error(f"  STILL PRESENT: {label}")
                all_ok = False
            else:
                ok(f"  absent (expected): {label}")

    # Market data cache MUST still be present (never deleted)
    cache_dir = base / "data_cache" / "bulk"
    if cache_dir.exists():
        ok(f"  market data cache preserved: data_cache/bulk/")
    else:
        warn(f"  data_cache/bulk/ is absent — this is unusual unless you ran Scenario E.")

    if all_ok:
        ok("All verification checks passed.")
    else:
        error("One or more checks failed — review errors above.")

    return all_ok

# ── Confirmation prompt ────────────────────────────────────────────────────────

def confirm(prompt: str, auto_yes: bool = False) -> bool:
    if auto_yes:
        info(f"Auto-confirmed: {prompt}")
        return True
    answer = input(f"{YELLOW}{prompt} [y/N]{RESET} ").strip().lower()
    return answer in ("y", "yes")

# ── Step 7 — Full Reset ────────────────────────────────────────────────────────

def run_full_reset(base: Path, backup_dir: Path, paths: dict,
                   no_backup: bool, auto_yes: bool, dry_run: bool) -> None:
    header("Step 7 — Full Portfolio Reset")
    print(
        f"{RED}{BOLD}WARNING:{RESET} This operation is irreversible.\n"
        "It permanently deletes ALL live portfolio state, trade history,\n"
        "execution logs, and generated reports.\n"
        "Market data and indicator caches are preserved.\n"
    )

    if not confirm("Proceed with FULL reset?", auto_yes):
        info("Aborted by user.")
        sys.exit(0)

    original_cron = None

    # Step 7 / Step 1 — Backup
    if not no_backup:
        backup_keys = [
            "portfolio_state", "trade_ledger", "portfolio_state_backups",
            "reports_executions", "reports_rebalancing", "reports_daily", "logs",
        ]
        create_backup(base, backup_dir, "full_reset", paths, backup_keys, dry_run)
    else:
        warn("Backup skipped (--no-backup). Proceeding directly to deletion.")

    # Step 7 / Step 2 — Suspend cron
    header("Step 2 — Suspend Cron Jobs")
    original_cron = suspend_cron(dry_run)

    # Step 7 / Step 3 — Delete portfolio state and trade history
    header("Step 3 — Delete Portfolio State & Trade History")
    for key in ("portfolio_state", "trade_ledger", "execution_template",
                "portfolio_state_backups"):
        delete_path(paths[key], dry_run)

    # Step 7 / Step 4 — Delete reports
    header("Step 4 — Delete Execution & Recommendation Reports")
    for key in ("reports_executions", "reports_rebalancing", "reports_daily"):
        delete_path(paths[key], dry_run)

    # Step 7 / Step 5 — Delete logs
    header("Step 5 — Delete Logs")
    glob_delete(paths["logs"], "*.log", dry_run)

    # Step 7 / Step 6 — Verify
    if not dry_run:
        verify_reset(base, paths, scenario="full")

    # Restore cron
    header("Restore Cron Jobs")
    restore_cron(original_cron, dry_run)

    ok("Full reset complete.")

# ── Step 8 — Selective Resets ─────────────────────────────────────────────────

def run_scenario_a(base: Path, paths: dict, auto_yes: bool, dry_run: bool) -> None:
    """Scenario A — Reset paper portfolio but keep trade history."""
    header("Scenario A — Reset Positions (keep trade ledger)")
    print("Archives portfolio_state.json, then zeroes it out.\n"
          "trade_ledger.jsonl is PRESERVED.")

    if not confirm("Proceed with Scenario A?", auto_yes):
        info("Aborted."); return

    src = paths["portfolio_state"]
    backup_dest = paths["portfolio_state_backups"]

    if src.exists():
        timestamp = datetime.now().strftime("%Y%m%d")
        dest = backup_dest / f"portfolio_state_{timestamp}.json"
        if not dry_run:
            backup_dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        ok(f"  archived → {dest}")

        info("  writing empty portfolio state …")
        if not dry_run:
            src.write_text("{}\n", encoding="utf-8")
        ok(f"  portfolio_state.json reset to empty object.")
    else:
        warn("  portfolio_state.json not found — nothing to reset.")


def run_scenario_b(base: Path, paths: dict, auto_yes: bool, dry_run: bool) -> None:
    """Scenario B — Clear only logs (keep portfolio and trade history)."""
    header("Scenario B — Clear Logs Only")
    print("Deletes all *.log files and daily report JSON/CSV.\n"
          "Portfolio state and trade ledger are PRESERVED.")

    if not confirm("Proceed with Scenario B?", auto_yes):
        info("Aborted."); return

    glob_delete(paths["logs"], "*.log", dry_run)
    glob_delete(paths["reports_daily"], "*.json", dry_run)
    glob_delete(paths["reports_daily"], "*.csv", dry_run)
    ok("Log and daily-report cleanup complete.")


def run_scenario_c(base: Path, paths: dict, auto_yes: bool, dry_run: bool) -> None:
    """Scenario C — Clear stale recommendation reports."""
    header("Scenario C — Clear Stale Recommendation Reports")
    print("Deletes reports/rebalancing/ then recreates the empty directory.\n"
          "Run Script 12 afterwards to regenerate fresh reports.")

    if not confirm("Proceed with Scenario C?", auto_yes):
        info("Aborted."); return

    delete_path(paths["reports_rebalancing"], dry_run)
    if not dry_run:
        paths["reports_rebalancing"].mkdir(parents=True, exist_ok=True)
        ok(f"  recreated empty: {paths['reports_rebalancing']}")

    info("Next step: run  python scripts/12_generate_recommendation_report.py")


def run_scenario_d(base: Path, paths: dict, auto_yes: bool, dry_run: bool) -> None:
    """Scenario D — Reset backtest and validation results only."""
    header("Scenario D — Reset Backtest & Validation Results")
    print("Deletes backtest, Monte Carlo, validation, OOS, and deployment caches\n"
          "and their corresponding reports.\n"
          "Live portfolio state is PRESERVED.")

    if not confirm("Proceed with Scenario D?", auto_yes):
        info("Aborted."); return

    cache_keys = ("cache_backtest", "cache_monte_carlo", "cache_validation",
                  "cache_oos_validation", "cache_deployment")
    report_keys = ("reports_backtest", "reports_validation",
                   "reports_oos_validation", "reports_deployment")

    for key in cache_keys + report_keys:
        delete_path(paths[key], dry_run)

    ok("Backtest / validation reset complete.")


def run_scenario_e(base: Path, backup_dir: Path, paths: dict,
                   no_backup: bool, auto_yes: bool, dry_run: bool) -> None:
    """Scenario E — Full system rebuild (nuclear option)."""
    header("Scenario E — Full System Rebuild (NUCLEAR)")
    print(
        f"{RED}{BOLD}DANGER:{RESET} This deletes EVERYTHING including market data.\n"
        "Re-downloading all historical data will take several hours.\n"
    )

    if not confirm("Type 'NUCLEAR' to confirm full system wipe: ", auto_yes=False):
        # Extra hard gate — require literal word even with --yes
        answer = input("Type NUCLEAR to confirm: ").strip()
        if answer != "NUCLEAR":
            info("Aborted."); return

    if not no_backup:
        info("Creating full system backup before nuclear wipe …")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = backup_dir / f"full_system_backup_{timestamp}.tar.gz"
        info(f"  → {archive_path}")
        if not dry_run:
            backup_dir.mkdir(parents=True, exist_ok=True)
            with tarfile.open(archive_path, "w:gz") as tar:
                tar.add(base, arcname=base.name)
            size_mb = archive_path.stat().st_size / 1_048_576
            ok(f"  Full backup complete ({size_mb:.1f} MB)")

    for key in ("data_dir", "data_cache_dir", "reports_dir", "logs_dir"):
        delete_path(paths[key], dry_run)

    # Recreate empty directory structure
    header("Recreating Empty Directory Structure")
    for d in ("data", "data_cache", "logs", "reports"):
        target = base / d
        if not dry_run:
            target.mkdir(parents=True, exist_ok=True)
        ok(f"  created: {target}")

    info("Next step: python scripts/01_download_eodhd_bulk.py --mode initial")
    ok("Nuclear rebuild complete. Run Script 01 to re-initialise market data.")

# ── Audit log ─────────────────────────────────────────────────────────────────

def write_audit_log(base: Path, action: str, scenario: str,
                    backup_path: str | None, dry_run: bool) -> None:
    """
    Append a structured record to logs/reset_audit.jsonl.
    This file is intentionally NOT deleted by any reset scenario so you
    retain a permanent record of every reset that was ever run.
    """
    log_dir = base / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    audit_file = log_dir / "reset_audit.jsonl"

    record = {
        "timestamp": datetime.now().isoformat(),
        "action": action,
        "scenario": scenario,
        "backup": backup_path,
        "dry_run": dry_run,
        "user": os.environ.get("USER", "unknown"),
    }
    if not dry_run:
        with open(audit_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    info(f"Audit record written → {audit_file}")

# ── CLI entry point ────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="TrendFollowingOS Portfolio Reset Utility (Steps 7 & 8)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--full", action="store_true",
                      help="Step 7 — full portfolio reset")
    mode.add_argument("--scenario", choices=["A", "B", "C", "D", "E"],
                      help="Step 8 — selective reset scenario")
    mode.add_argument("--verify-only", action="store_true",
                      help="Run post-reset verification without deleting")

    p.add_argument("--base-dir",   type=Path,
                   default=Path.home() / "Desktop" / "Trade" / "trend_strategy_dev",
                   help="Project root directory")
    p.add_argument("--backup-dir", type=Path,
                   default=Path.home() / "Desktop",
                   help="Backup destination directory")
    p.add_argument("--no-backup",  action="store_true",
                   help="Skip backup step (not recommended)")
    p.add_argument("--yes",        action="store_true",
                   help="Auto-confirm all prompts")
    p.add_argument("--dry-run",    action="store_true",
                   help="Show what would be deleted without deleting")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    base: Path = args.base_dir.expanduser().resolve()
    backup_dir: Path = args.backup_dir.expanduser().resolve()
    paths = build_paths(base)

    print(f"\n{BOLD}TrendFollowingOS — Portfolio Reset Utility{RESET}")
    print(f"  Base directory : {base}")
    print(f"  Backup directory: {backup_dir}")
    if args.dry_run:
        print(f"  {YELLOW}DRY-RUN mode — no files will be deleted{RESET}")

    if not base.exists():
        error(f"Base directory does not exist: {base}")
        sys.exit(1)

    # ── Dispatch ───────────────────────────────────────────────────────────────
    if args.verify_only:
        verify_reset(base, paths, scenario="full")
        return

    if args.full:
        run_full_reset(base, backup_dir, paths,
                       args.no_backup, args.yes, args.dry_run)
        write_audit_log(base, "full_reset", "7",
                        str(backup_dir) if not args.no_backup else None,
                        args.dry_run)

    elif args.scenario:
        dispatch = {
            "A": lambda: run_scenario_a(base, paths, args.yes, args.dry_run),
            "B": lambda: run_scenario_b(base, paths, args.yes, args.dry_run),
            "C": lambda: run_scenario_c(base, paths, args.yes, args.dry_run),
            "D": lambda: run_scenario_d(base, paths, args.yes, args.dry_run),
            "E": lambda: run_scenario_e(base, backup_dir, paths,
                                        args.no_backup, args.yes, args.dry_run),
        }
        dispatch[args.scenario]()
        write_audit_log(base, f"scenario_{args.scenario}", f"8{args.scenario}",
                        None, args.dry_run)


if __name__ == "__main__":
    main()
