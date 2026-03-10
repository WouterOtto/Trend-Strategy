#!/usr/bin/env python3
"""
Script 13: Execution Logger
============================
Record actual broker fills, update portfolio state, and compute slippage.

Purpose:
    After the portfolio manager executes the recommendations produced by
    Script 11, this script ingests the ACTUAL fill prices and quantities,
    performs four tasks:

        1. Validates every trade against business rules (no zero-share fills,
           no negative prices, date sanity, duplicate detection).
        2. Updates data/portfolio_state.json â the single source of truth for
           the live portfolio consumed by Scripts 09, 10, and 11.
        3. Appends an immutable record to data/trade_ledger.jsonl â the
           append-only audit trail used for performance attribution.
        4. Computes per-trade slippage vs the Script 11 recommended limit
           price and saves a session execution report.

    The script intentionally contains NO strategy logic.  It is purely a
    data-entry and bookkeeping tool.  All recommendations remain the
    responsibility of Scripts 07–11.

    â   Run AFTER broker confirmations are in hand.
       Do NOT run before all fills for a session are known.

Input modes:
    MODE 1: Single trade via CLI flags
        Use for one-off entries (stop-loss hit intraday, partial fill, etc.)

    MODE 2: Batch CSV file (recommended for monthly rebalancing)
        Use a CSV with columns matching the trade schema (see --batch-file).
        Template generated automatically on first run.

    MODE 3: Auto-import from Script 11 recommendations (dry-run reconcile)
        Reads the latest recommendations JSON and pre-populates a batch CSV
        template for the manager to fill in actual prices.
        Use --generate-template to produce the CSV, then fill in fill_price
        and re-run with --batch-file.

CLI flag reference:
    --execution-date    YYYY-MM-DD (required for single-trade mode;
                        optional for batch mode if CSV has transaction_date column)
    --action            BUY | SELL
    --symbol            e.g. AAPL.US
    --shares            Integer number of shares executed
    --fill-price        Actual broker fill price (EUR)
    --commission        Broker commission in EUR (default 0.0)
    --broker-ref        Optional broker order reference string
    --notes             Optional free-text annotation
    --batch-file        Path to CSV with multiple trades
    --rebalancing-ref   Path to Script 11 JSON (for slippage cross-reference)
    --generate-template Generate blank batch CSV from latest Script 11 output
    --dry-run           Validate and report without writing any files

Inputs:
    data/portfolio_state.json                            (current live positions)
    data/trade_ledger.jsonl                              (existing ledger, if any)
    reports/rebalancing/{YYYY-MM}_recommendations.json   (Script 11, optional)

Outputs:
    data/portfolio_state.json                            (UPDATED)
    data/trade_ledger.jsonl                              (APPENDED)
    reports/executions/{YYYYMMDD}_execution_log.json     (session report)
    reports/executions/{YYYYMMDD}_execution_log.csv      (human-readable)
    reports/executions/{YYYYMMDD}_batch_template.csv     (--generate-template only)
    logs/log_execution_{timestamp}.log

Execution:
    # Single BUY
    python scripts/13_log_execution.py \\
        --execution-date 2026-02-03 \\
        --action BUY \\
        --symbol AAPL.US \\
        --shares 10 \\
        --fill-price 152.45 \\
        --commission 9.95 \\
        --broker-ref "ORD-20260203-001"

    # Monthly batch with global execution date
    python scripts/13_log_execution.py \\
        --execution-date 2026-02-03 \\
        --batch-file reports/executions/2026-02-03_fills.csv \\
        --rebalancing-ref reports/rebalancing/2026-01_recommendations.json

    # Batch with per-row dates (no --execution-date needed)
    python scripts/13_log_execution.py \\
        --batch-file reports/executions/multi_day_fills.csv \\
        --rebalancing-ref reports/rebalancing/2026-01_recommendations.json

    # Generate a pre-filled template from Script 11 output
    python scripts/13_log_execution.py \\
        --generate-template \\
        --rebalancing-ref reports/rebalancing/2026-01_recommendations.json

    # Validate without writing
    python scripts/13_log_execution.py \\
        --execution-date 2026-02-03 \\
        --batch-file reports/executions/2026-02-03_fills.csv \\
        --dry-run

Architecture: v3.2 (Feb 2026)
"""

import csv
import json
import logging
import argparse
import sys
import uuid
from copy import deepcopy
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ============================================================================
# PATH CONSTANTS
# ============================================================================

PROJECT_ROOT   = Path(__file__).parent.parent
DATA_DIR       = PROJECT_ROOT / "data"
REPORTS_DIR    = PROJECT_ROOT / "reports"
REBALANCING_DIR = REPORTS_DIR / "rebalancing"
EXECUTIONS_DIR = REPORTS_DIR / "executions"
LOG_DIR        = PROJECT_ROOT / "logs"

PORTFOLIO_STATE_FILE = DATA_DIR / "portfolio_state.json"
TRADE_LEDGER_FILE    = DATA_DIR / "trade_ledger.jsonl"

# ============================================================================
# CONSTANTS
# ============================================================================

VALID_ACTIONS      = {"BUY", "SELL"}
BATCH_CSV_COLUMNS  = [
    "action",
    "symbol",
    "shares",
    "fill_price",
    "transaction_date",  # optional - falls back to --execution-date if empty
    "commission",
    "broker_ref",
    "notes",
]
TEMPLATE_CSV_COLUMNS = BATCH_CSV_COLUMNS  # same schema, pre-populated with 0.0 for fill_price

MAX_COMMISSION_PCT   = 5.0   # warn if commission > 5% of trade value (likely data-entry error)
MAX_SLIPPAGE_WARN_PCT = 2.0  # warn if slippage vs recommended price > 2%

# ============================================================================
# LOGGING SETUP
# ============================================================================

logger = logging.getLogger("execution_logger")


def setup_logging(execution_date: Optional[str] = None) -> logging.Logger:
    """Configure rotating file + console logging."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file  = LOG_DIR / f"log_execution_{timestamp}.log"

    fmt = "%(asctime)s [%(levelname)s] %(message)s"

    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    global logger
    logger = logging.getLogger("execution_logger")
    return logger


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class TradeRecord:
    """
    Canonical representation of a single executed trade.

    Populated from either CLI args or a CSV row.  All monetary values are
    expressed in EUR (the base currency of the strategy).
    """
    action:         str              # "BUY" | "SELL"
    symbol:         str              # e.g. "AAPL.US"
    shares:         int              # number of whole shares
    fill_price:     float            # actual broker fill price
    execution_date: str              # "YYYY-MM-DD"
    commission:     float = 0.0      # broker commission in EUR
    broker_ref:     str   = ""       # broker order reference
    notes:          str   = ""       # free-text annotation

    # Computed fields (populated by enrich_trade_record)
    trade_id:            str   = field(default_factory=lambda: str(uuid.uuid4())[:12])
    logged_at:           str   = field(default_factory=lambda: datetime.now().isoformat())
    gross_value_eur:     float = 0.0
    net_value_eur:       float = 0.0  # gross + commission (for buys) / gross - commission (sells)
    recommended_price:   Optional[float] = None
    slippage_eur:        Optional[float] = None
    slippage_pct:        Optional[float] = None
    slippage_direction:  Optional[str]   = None   # "FAVORABLE" | "ADVERSE" | "NEUTRAL"
    exit_reason:         Optional[str]   = None   # populated for SELL trades
    realized_pnl_eur:    Optional[float] = None   # populated for SELL trades
    realized_pnl_pct:    Optional[float] = None   # populated for SELL trades


@dataclass
class SessionSummary:
    """Aggregate statistics for the execution session."""
    execution_date:       str
    total_trades:         int = 0
    buy_count:            int = 0
    sell_count:           int = 0
    total_gross_eur:      float = 0.0
    total_commissions_eur: float = 0.0
    total_realized_pnl_eur: float = 0.0
    adverse_slippage_count: int = 0
    favorable_slippage_count: int = 0
    avg_slippage_pct:     Optional[float] = None
    warnings:             List[str] = field(default_factory=list)
    errors:               List[str] = field(default_factory=list)


# ============================================================================
# INPUT PARSERS
# ============================================================================

def parse_single_trade(args: argparse.Namespace) -> TradeRecord:
    """
    Build a TradeRecord from individual CLI flags.

    All required flags are enforced here; argparse already guaranteed they
    were supplied.
    """
    return TradeRecord(
        action         = args.action.upper(),
        symbol         = args.symbol.upper().strip(),
        shares         = args.shares,
        fill_price     = args.fill_price,
        execution_date = args.execution_date,
        commission     = args.commission or 0.0,
        broker_ref     = args.broker_ref or "",
        notes          = args.notes or "",
    )


def parse_batch_csv(csv_path: Path, execution_date: Optional[str]) -> List[TradeRecord]:
    """
    Parse a batch fill CSV into a list of TradeRecord objects.

    Expected columns (order-independent, case-insensitive header):
        action, symbol, shares, fill_price, transaction_date (optional),
        commission, broker_ref, notes

    Rows with fill_price == 0 or empty are skipped with a WARNING (unfilled
    orders are a valid outcome â limit orders not filled before EOD).

    Date handling:
        If the CSV has a 'transaction_date' column and the cell is non-empty,
        that date is used for the row (format: YYYY-MM-DD).
        Otherwise, execution_date (from CLI --execution-date) is used.
        At least one source of date must be provided.

    Args:
        csv_path:       Path to the CSV file.
        execution_date: Default date for rows without transaction_date (optional).

    Returns:
        List of parsed TradeRecord objects.

    Raises:
        SystemExit on missing file, malformed header, or date errors.
    """
    if not csv_path.exists():
        logger.error(f"Batch CSV not found: {csv_path}")
        sys.exit(1)

    records: List[TradeRecord] = []
    skipped_unfilled = 0

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        # Normalise header to lowercase
        if reader.fieldnames is None:
            logger.error("Batch CSV is empty.")
            sys.exit(1)

        header = [h.strip().lower() for h in reader.fieldnames]
        
        # transaction_date is optional - only required columns are checked
        required = [c for c in BATCH_CSV_COLUMNS if c != "transaction_date"]
        missing = [c for c in required if c not in header]
        if missing:
            logger.error(
                f"Batch CSV is missing required columns: {missing}\n"
                f"Found: {header}"
            )
            sys.exit(1)

        has_date_column = "transaction_date" in header

        for line_num, row in enumerate(reader, start=2):
            # Normalise keys
            row = {k.strip().lower(): v.strip() for k, v in row.items()}

            fill_price_raw = row.get("fill_price", "").strip()

            # Skip unfilled orders (fill_price blank or 0)
            if not fill_price_raw or float(fill_price_raw) == 0.0:
                symbol = row.get("symbol", f"row-{line_num}")
                logger.warning(
                    f"  Line {line_num} [{symbol}]: fill_price is 0 or blank â "
                    "treating as UNFILLED ORDER, skipped."
                )
                skipped_unfilled += 1
                continue

            # Determine date for this row
            row_date = None
            if has_date_column:
                date_str = row.get("transaction_date", "").strip()
                if date_str:
                    try:
                        datetime.strptime(date_str, "%Y-%m-%d")
                        row_date = date_str
                    except ValueError:
                        logger.error(
                            f"  Line {line_num}: transaction_date '{date_str}' is not "
                            "a valid YYYY-MM-DD date."
                        )
                        sys.exit(1)
            
            if row_date is None:
                if execution_date is None:
                    logger.error(
                        f"  Line {line_num}: No transaction_date in CSV and no "
                        "--execution-date provided. At least one date source is required."
                    )
                    sys.exit(1)
                row_date = execution_date

            try:
                records.append(TradeRecord(
                    action         = row["action"].upper(),
                    symbol         = row["symbol"].upper(),
                    shares         = int(float(row["shares"])),
                    fill_price     = float(row["fill_price"]),
                    execution_date = row_date,
                    commission     = float(row.get("commission") or 0),
                    broker_ref     = row.get("broker_ref", ""),
                    notes          = row.get("notes", ""),
                ))
            except (ValueError, KeyError) as exc:
                logger.error(f"  Line {line_num}: cannot parse row â {exc}. Row: {dict(row)}")
                sys.exit(1)

    if skipped_unfilled:
        logger.info(f"  {skipped_unfilled} row(s) skipped (unfilled / zero fill_price).")

    logger.info(f"  Parsed {len(records)} trade(s) from {csv_path.name}")
    return records


# ============================================================================
# VALIDATION
# ============================================================================

def validate_trade(trade: TradeRecord, existing_positions: Dict) -> List[str]:
    """
    Apply business-rule validation to a single trade.

    Returns a list of error strings.  An empty list means the trade is valid.

    Rules enforced:
        V1  action must be BUY or SELL
        V2  symbol must be non-empty and contain a dot (exchange suffix)
        V3  shares must be â¥ 1
        V4  fill_price must be > 0
        V5  commission must be â¥ 0
        V6  execution_date must parse as YYYY-MM-DD and not be in the future
        V7  SELL requires an existing position in portfolio_state
        V8  SELL shares must not exceed held shares (warn, not block)
        V9  commission must not exceed MAX_COMMISSION_PCT of gross value
        V10 BUY on a symbol already held is allowed but triggers a WARNING
            (dollar-cost averaging is not part of this strategy)
    """
    errors: List[str] = []

    # V1 – action
    if trade.action not in VALID_ACTIONS:
        errors.append(f"V1: action '{trade.action}' must be BUY or SELL.")

    # V2 – symbol
    if not trade.symbol:
        errors.append("V2: symbol is empty.")
    elif "." not in trade.symbol:
        errors.append(
            f"V2: symbol '{trade.symbol}' has no exchange suffix (expected e.g. AAPL.US)."
        )

    # V3 – shares
    if trade.shares < 1:
        errors.append(f"V3: shares must be â¥ 1, got {trade.shares}.")

    # V4 – fill_price
    if trade.fill_price <= 0:
        errors.append(f"V4: fill_price must be > 0, got {trade.fill_price}.")

    # V5 – commission
    if trade.commission < 0:
        errors.append(f"V5: commission must be â¥ 0, got {trade.commission}.")

    # V6 – date
    try:
        exec_date = datetime.strptime(trade.execution_date, "%Y-%m-%d").date()
        if exec_date > date.today():
            errors.append(
                f"V6: execution_date {trade.execution_date} is in the future. "
                "Post-date logging is not permitted."
            )
    except ValueError:
        errors.append(
            f"V6: execution_date '{trade.execution_date}' is not a valid YYYY-MM-DD date."
        )

    # V7 / V8 – SELL requires held position
    if trade.action == "SELL":
        if trade.symbol not in existing_positions:
            errors.append(
                f"V7: SELL on '{trade.symbol}' but no position found in portfolio_state.json. "
                "Verify symbol or check for a prior SELL that already removed it."
            )
        else:
            held_shares = existing_positions[trade.symbol].get("shares", 0)
            if trade.shares > held_shares:
                errors.append(
                    f"V8: SELL {trade.shares} shares of '{trade.symbol}' but only "
                    f"{held_shares} held.  Partial oversell detected."
                )

    # V9 – commission sanity
    if trade.fill_price > 0 and trade.shares >= 1:
        gross = trade.fill_price * trade.shares
        if gross > 0 and (trade.commission / gross * 100) > MAX_COMMISSION_PCT:
            errors.append(
                f"V9: commission {trade.commission:.2f} EUR is "
                f"{trade.commission / gross * 100:.1f}% of gross value {gross:.2f} EUR â "
                f"exceeds {MAX_COMMISSION_PCT}% sanity cap. Verify the commission figure."
            )

    return errors


def check_duplicate(trade: TradeRecord, ledger: List[Dict]) -> bool:
    """
    Check whether an identical trade already exists in the ledger.

    Duplicates are identified by (execution_date, action, symbol, shares,
    fill_price, broker_ref).  broker_ref collision alone is not sufficient
    because it may be absent.

    Returns True if duplicate found.
    """
    for entry in ledger:
        if (
            entry.get("execution_date") == trade.execution_date
            and entry.get("action")         == trade.action
            and entry.get("symbol")         == trade.symbol
            and entry.get("shares")         == trade.shares
            and abs(entry.get("fill_price", 0) - trade.fill_price) < 0.0001
            and (
                not trade.broker_ref
                or entry.get("broker_ref") == trade.broker_ref
            )
        ):
            return True
    return False


# ============================================================================
# ENRICHMENT
# ============================================================================

def enrich_trade_record(
    trade:            TradeRecord,
    existing_positions: Dict,
    recommendations:  Optional[Dict],
) -> TradeRecord:
    """
    Compute derived monetary fields and slippage vs Script 11 recommendation.

    Mutates and returns the trade record in place.

    Gross / net value:
        BUY:  gross = shares Ã fill_price
              net   = gross + commission  (cash outflow)
        SELL: gross = shares Ã fill_price
              net   = gross â commission  (cash inflow)

    Slippage (BUY only, vs recommended limit price):
        slippage_eur = (fill_price â recommended_price) Ã shares
        slippage_pct = (fill_price â recommended_price) / recommended_price Ã 100

        Positive slippage = paid more than recommended → ADVERSE
        Negative slippage = paid less than recommended → FAVORABLE

    Realized P&L (SELL only):
        pnl_eur = (fill_price â entry_price) Ã shares â commission
        pnl_pct = pnl_eur / (entry_price Ã shares) Ã 100
    """
    trade.gross_value_eur = round(trade.fill_price * trade.shares, 4)

    if trade.action == "BUY":
        trade.net_value_eur = round(trade.gross_value_eur + trade.commission, 4)
    else:
        trade.net_value_eur = round(trade.gross_value_eur - trade.commission, 4)

    # ââ Slippage vs recommendation âââââââââââââââââââââââââââââââââââââââââ
    if recommendations and trade.action == "BUY":
        rec_price = _find_recommended_price(trade.symbol, recommendations)
        if rec_price:
            trade.recommended_price  = rec_price
            raw_slip                 = trade.fill_price - rec_price
            trade.slippage_eur       = round(raw_slip * trade.shares, 4)
            trade.slippage_pct       = round(raw_slip / rec_price * 100, 4)
            if abs(trade.slippage_pct) < 0.01:
                trade.slippage_direction = "NEUTRAL"
            elif trade.slippage_pct > 0:
                trade.slippage_direction = "ADVERSE"
            else:
                trade.slippage_direction = "FAVORABLE"

    # ââ Realized P&L for SELL ââââââââââââââââââââââââââââââââââââââââââââââ
    if trade.action == "SELL":
        position = existing_positions.get(trade.symbol, {})
        entry_price = position.get("entry_price")
        if entry_price:
            pnl = (trade.fill_price - entry_price) * trade.shares - trade.commission
            cost_basis = entry_price * trade.shares
            trade.realized_pnl_eur = round(pnl, 4)
            trade.realized_pnl_pct = round(pnl / cost_basis * 100, 4) if cost_basis else None

        # Capture exit reason from the position record if available
        exit_signal = _find_exit_reason(trade.symbol, recommendations)
        trade.exit_reason = exit_signal

    return trade


def _find_recommended_price(symbol: str, recommendations: Dict) -> Optional[float]:
    """
    Extract the recommended limit price for a BUY from Script 11 output.

    Searches entries.new[] for a matching symbol and returns
    entry["limit_price"] or entry["close_price"] * 1.005 as fallback.
    """
    new_entries = recommendations.get("entries", {}).get("new", [])
    for entry in new_entries:
        if entry.get("symbol") == symbol:
            lp = entry.get("limit_price") or entry.get("entry_limit_price")
            if lp:
                return float(lp)
            cp = entry.get("close_price") or entry.get("recommended_close")
            if cp:
                return round(float(cp) * 1.005, 4)  # +0.5% convention
    return None


def _find_exit_reason(symbol: str, recommendations: Optional[Dict]) -> Optional[str]:
    """Extract the exit reason from Script 11 mandatory/rotation exits."""
    if not recommendations:
        return None
    for exit_list in (
        recommendations.get("exits", {}).get("mandatory", []),
        recommendations.get("exits", {}).get("rotation",  []),
    ):
        for item in exit_list:
            if item.get("symbol") == symbol:
                return item.get("reason", "")
    return None


# ============================================================================
# PORTFOLIO STATE MANAGER
# ============================================================================

def load_portfolio_state() -> Dict:
    """
    Load current portfolio_state.json.

    Supports both flat {"SYMBOL": {...}} and nested {"positions": {...}}.
    Returns empty dict on first run (no positions yet).
    """
    if not PORTFOLIO_STATE_FILE.exists():
        logger.info("portfolio_state.json not found â assuming empty portfolio (first run).")
        return {}

    with open(PORTFOLIO_STATE_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if not isinstance(raw, dict):
        logger.error("portfolio_state.json has unexpected format. Expected a JSON object.")
        sys.exit(1)

    # Normalise layout B (nested under "positions")
    if "positions" in raw and isinstance(raw.get("positions"), dict):
        raw = raw["positions"]

    # Strip metadata keys
    positions = {k: v for k, v in raw.items() if not k.startswith("_")}
    logger.info(f"  Loaded {len(positions)} existing position(s) from portfolio_state.json")
    return positions


def apply_trade_to_portfolio(
    portfolio:  Dict,
    trade:      TradeRecord,
) -> Dict:
    """
    Apply a single validated trade to the portfolio state dict.

    BUY:  Insert or update position record.
          If the symbol is already held (partial fill / cost-averaging),
          the position is updated using a weighted average entry price and
          total shares.  A WARNING is emitted because this is non-standard.

    SELL: Remove the position entirely (full exit only).
          Partial SELL (shares < held) is supported: the position record is
          updated with the remaining shares and the original entry price is
          preserved.

    Returns the mutated portfolio dict.
    """
    if trade.action == "BUY":
        if trade.symbol in portfolio:
            existing = portfolio[trade.symbol]
            held_shares  = existing.get("shares", 0)
            held_price   = existing.get("entry_price", trade.fill_price)

            # Weighted average entry price
            total_shares = held_shares + trade.shares
            avg_price    = (
                (held_price * held_shares + trade.fill_price * trade.shares)
                / total_shares
            )
            logger.warning(
                f"  [{trade.symbol}] BUY on existing position â "
                f"cost-averaging {held_shares} → {total_shares} shares, "
                f"avg entry {avg_price:.4f} (was {held_price:.4f}). "
                "Not standard for this strategy â verify intent."
            )
            portfolio[trade.symbol]["shares"]      = total_shares
            portfolio[trade.symbol]["entry_price"] = round(avg_price, 4)
            portfolio[trade.symbol]["is_new_entry"] = False
        else:
            portfolio[trade.symbol] = {
                "entry_price":           trade.fill_price,
                "entry_date":            trade.execution_date,
                "shares":                trade.shares,
                "current_value":         round(trade.fill_price * trade.shares, 2),
                "unrealized_pnl":        0.0,
                "unrealized_pnl_pct":    0.0,
                "current_stop_price":    None,  # Set by Script 08 on Friday
                "initial_stop_price":    None,  # Set by Script 08 on Friday
                "trailing_stop_price":   None,
                "stop_type":             "initial",
                "stop_last_update_date": None,
                "data_last_update":      datetime.now().isoformat(),
                "is_new_entry":          True,
                "commission_entry":      trade.commission,
                "broker_ref_entry":      trade.broker_ref,
            }

    elif trade.action == "SELL":
        if trade.symbol in portfolio:
            existing     = portfolio[trade.symbol]
            held_shares  = existing.get("shares", 0)
            remaining    = held_shares - trade.shares

            if remaining <= 0:
                # Full exit â remove the position
                del portfolio[trade.symbol]
                logger.info(
                    f"  [{trade.symbol}] Full exit â position removed from portfolio state."
                )
            else:
                # Partial exit â reduce shares, keep entry price
                portfolio[trade.symbol]["shares"]        = remaining
                portfolio[trade.symbol]["data_last_update"] = datetime.now().isoformat()
                portfolio[trade.symbol]["commission_exit_partial"] = trade.commission
                logger.info(
                    f"  [{trade.symbol}] Partial exit â {trade.shares} sold, "
                    f"{remaining} remaining."
                )
        else:
            # Should have been caught by validation, but guard defensively
            logger.error(
                f"  [{trade.symbol}] SELL attempted but no position found. "
                "Portfolio state unchanged for this symbol."
            )

    return portfolio


def save_portfolio_state(portfolio: Dict, dry_run: bool = False) -> None:
    """
    Persist the updated portfolio state.

    Writes with a backup strategy:
        1. Write to portfolio_state.json.tmp
        2. Rename to portfolio_state.json (atomic on POSIX)
        3. Keep a timestamped backup in data/portfolio_state_backups/

    Args:
        portfolio: Dict keyed by symbol.
        dry_run:   If True, skip file writes.
    """
    if dry_run:
        logger.info("  [DRY RUN] portfolio_state.json would be updated.")
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Timestamped backup
    backup_dir = DATA_DIR / "portfolio_state_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if PORTFOLIO_STATE_FILE.exists():
        backup_path = backup_dir / f"portfolio_state_{timestamp}.json"
        import shutil
        shutil.copy2(PORTFOLIO_STATE_FILE, backup_path)
        logger.info(f"  Backup → {backup_path}")

    # Atomic write via tmp file
    tmp_path = PORTFOLIO_STATE_FILE.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(portfolio, f, indent=2, default=str)
    tmp_path.replace(PORTFOLIO_STATE_FILE)

    logger.info(
        f"â portfolio_state.json updated â {len(portfolio)} position(s) held."
    )


# ============================================================================
# TRADE LEDGER
# ============================================================================

def load_trade_ledger() -> List[Dict]:
    """Load existing ledger entries from JSONL file. Returns [] on first run."""
    if not TRADE_LEDGER_FILE.exists():
        return []

    entries: List[Dict] = []
    with open(TRADE_LEDGER_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    logger.warning(f"  Malformed ledger line skipped: {exc}")
    logger.info(f"  Loaded {len(entries)} existing ledger entry/entries.")
    return entries


def append_to_ledger(trades: List[TradeRecord], dry_run: bool = False) -> None:
    """
    Append trade records to the JSONL ledger (append-only; never overwrites).

    Each record is a single JSON object on one line.  The file grows
    monotonically and is the ground-truth audit trail for the strategy.

    Args:
        trades:  List of enriched TradeRecord objects.
        dry_run: If True, skip file writes.
    """
    if dry_run:
        logger.info(f"  [DRY RUN] {len(trades)} trade(s) would be appended to ledger.")
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with open(TRADE_LEDGER_FILE, "a", encoding="utf-8") as f:
        for trade in trades:
            f.write(json.dumps(asdict(trade), default=str) + "\n")

    logger.info(f"â {len(trades)} trade(s) appended to trade_ledger.jsonl")


# ============================================================================
# RECOMMENDATIONS LOADER
# ============================================================================

def load_recommendations(ref_path: Optional[Path]) -> Optional[Dict]:
    """
    Load Script 11 recommendations JSON for slippage cross-referencing.

    If ref_path is None, attempt to auto-detect the most recent file in
    reports/rebalancing/.

    Returns None if no file is found (slippage analysis is skipped).
    """
    target = ref_path

    if target is None:
        if REBALANCING_DIR.exists():
            candidates = sorted(REBALANCING_DIR.glob("*_recommendations.json"), reverse=True)
            if candidates:
                target = candidates[0]
                logger.info(
                    f"  Auto-detected recommendations file: {target.name}"
                )

    if target is None:
        logger.info(
            "  No Script 11 recommendations file found â slippage analysis skipped."
        )
        return None

    if not target.exists():
        logger.warning(
            f"  Recommendations file not found: {target} â slippage analysis skipped."
        )
        return None

    with open(target, "r", encoding="utf-8") as f:
        data = json.load(f)

    logger.info(f"  Loaded recommendations from {target.name}")
    return data


# ============================================================================
# TEMPLATE GENERATOR
# ============================================================================

def generate_batch_template(recommendations: Dict, output_path: Path) -> None:
    """
    Generate a pre-populated batch CSV template from Script 11 recommendations.

    Exits and entries are written as rows with fill_price = 0.0 (unfilled).
    The manager fills in actual prices before re-running with --batch-file.

    Columns:
        action, symbol, shares, fill_price (=0.0), transaction_date (empty),
        commission (=0.0), broker_ref, notes

    Args:
        recommendations: Dict from Script 11.
        output_path:     Destination CSV path.
    """
    EXECUTIONS_DIR.mkdir(parents=True, exist_ok=True)
    rows: List[Dict] = []

    # Priority 1: mandatory exits (market open)
    for item in recommendations.get("exits", {}).get("mandatory", []):
        rows.append({
            "action":           "SELL",
            "symbol":           item.get("symbol", ""),
            "shares":           item.get("shares", 0),
            "fill_price":       "0.0",    # to be filled in
            "transaction_date": "",       # to be filled in (or use --execution-date)
            "commission":       "0.0",
            "broker_ref":       "",
            "notes":            f"EXIT-MANDATORY: {item.get('reason', '')} | {item.get('detail', '')}",
        })

    # Priority 2: rotation exits (month-end close)
    for item in recommendations.get("exits", {}).get("rotation", []):
        rows.append({
            "action":           "SELL",
            "symbol":           item.get("symbol", ""),
            "shares":           item.get("shares", 0),
            "fill_price":       "0.0",
            "transaction_date": "",
            "commission":       "0.0",
            "broker_ref":       "",
            "notes":            f"EXIT-ROTATION: rank={item.get('current_rank', 'N/A')}",
        })

    # Priority 3: new entries (limit orders)
    for item in recommendations.get("entries", {}).get("new", []):
        close   = item.get("close_price") or item.get("recommended_close") or 0.0
        limit   = round(float(close) * 1.005, 4) if close else 0.0
        rows.append({
            "action":           "BUY",
            "symbol":           item.get("symbol", ""),
            "shares":           item.get("shares", 0),
            "fill_price":       "0.0",
            "transaction_date": "",
            "commission":       "0.0",
            "broker_ref":       "",
            "notes":            (
                f"ENTRY: rank={item.get('momentum_rank', 'N/A')} | "
                f"limitâ{limit:.4f}"
            ),
        })

    if not rows:
        logger.warning("No exits or entries found in recommendations â template will be empty.")

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TEMPLATE_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nâ Template generated → {output_path}")
    print(
        f"  {len(rows)} row(s) written: "
        f"{sum(1 for r in rows if r['action']=='SELL')} SELL(s), "
        f"{sum(1 for r in rows if r['action']=='BUY')} BUY(s).\n"
        "  Fill in the 'fill_price' column with actual broker prices,\n"
        "  optionally fill 'transaction_date' for each row (or use --execution-date),\n"
        "  then re-run:\n"
        f"    python scripts/13_log_execution.py \\\n"
        f"        --batch-file {output_path}"
    )


# ============================================================================
# SESSION REPORT
# ============================================================================

def build_session_summary(
    trades:   List[TradeRecord],
    warnings: List[str],
    errors:   List[str],
    execution_date: str,
) -> SessionSummary:
    """Compute aggregate statistics for the logging session."""
    summary = SessionSummary(execution_date=execution_date)
    summary.warnings = warnings
    summary.errors   = errors

    slippage_pcts = []

    for trade in trades:
        summary.total_trades          += 1
        summary.total_gross_eur       += trade.gross_value_eur
        summary.total_commissions_eur += trade.commission

        if trade.action == "BUY":
            summary.buy_count += 1
        elif trade.action == "SELL":
            summary.sell_count += 1
            if trade.realized_pnl_eur is not None:
                summary.total_realized_pnl_eur += trade.realized_pnl_eur

        if trade.slippage_direction == "ADVERSE":
            summary.adverse_slippage_count   += 1
        elif trade.slippage_direction == "FAVORABLE":
            summary.favorable_slippage_count += 1

        if trade.slippage_pct is not None:
            slippage_pcts.append(trade.slippage_pct)

    if slippage_pcts:
        summary.avg_slippage_pct = round(
            sum(slippage_pcts) / len(slippage_pcts), 4
        )

    return summary


def save_execution_report(
    trades:       List[TradeRecord],
    summary:      SessionSummary,
    portfolio:    Dict,
    execution_date: str,
    dry_run:      bool = False,
) -> None:
    """
    Save the JSON and CSV execution reports for the session.

    JSON format:
        {
            "metadata": { ...session summary... },
            "portfolio_after": { symbol: position, ... },
            "trades": [ { ...TradeRecord fields... }, ... ]
        }

    CSV format (one row per trade):
        trade_id, execution_date, action, symbol, shares, fill_price,
        gross_value_eur, commission, net_value_eur, recommended_price,
        slippage_eur, slippage_pct, slippage_direction,
        realized_pnl_eur, realized_pnl_pct, exit_reason, broker_ref, notes
    """
    if dry_run:
        logger.info("  [DRY RUN] Execution report would be saved.")
        return

    EXECUTIONS_DIR.mkdir(parents=True, exist_ok=True)

    date_tag  = execution_date.replace("-", "")
    json_path = EXECUTIONS_DIR / f"{date_tag}_execution_log.json"
    csv_path  = EXECUTIONS_DIR / f"{date_tag}_execution_log.csv"

    # ââ JSON âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    output = {
        "metadata":        asdict(summary),
        "portfolio_after": portfolio,
        "trades":          [asdict(t) for t in trades],
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, default=str)
    logger.info(f"â Execution log (JSON) → {json_path}")

    # ââ CSV ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    csv_fields = [
        "trade_id", "execution_date", "action", "symbol", "shares",
        "fill_price", "gross_value_eur", "commission", "net_value_eur",
        "recommended_price", "slippage_eur", "slippage_pct", "slippage_direction",
        "realized_pnl_eur", "realized_pnl_pct", "exit_reason",
        "broker_ref", "notes",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        for trade in trades:
            writer.writerow(asdict(trade))
    logger.info(f"â Execution log (CSV)  → {csv_path}")


# ============================================================================
# HUMAN-READABLE CONSOLE SUMMARY
# ============================================================================

def print_session_summary(
    trades:  List[TradeRecord],
    summary: SessionSummary,
    dry_run: bool,
) -> None:
    """Print a clean session summary to stdout for the portfolio manager."""
    tag = " [DRY RUN]" if dry_run else ""
    print()
    print("=" * 72)
    print(f"  EXECUTION LOG â {summary.execution_date}{tag}")
    print("=" * 72)
    print(f"  Trades logged : {summary.total_trades}"
          f"  ({summary.buy_count} BUY, {summary.sell_count} SELL)")
    print(f"  Gross volume  : â¬{summary.total_gross_eur:,.2f}")
    print(f"  Commissions   : â¬{summary.total_commissions_eur:,.2f}")
    if summary.sell_count:
        pnl_str = (
            f"â¬{summary.total_realized_pnl_eur:+,.2f}"
            if summary.total_realized_pnl_eur else "n/a"
        )
        print(f"  Realized P&L  : {pnl_str}")
    if summary.avg_slippage_pct is not None:
        print(
            f"  Avg slippage  : {summary.avg_slippage_pct:+.4f}%  "
            f"({summary.adverse_slippage_count} adverse, "
            f"{summary.favorable_slippage_count} favorable)"
        )

    if trades:
        print()
        print(f"  {'ACTION':<6} {'SYMBOL':<16} {'SH':>5} {'FILL':>10} "
              f"{'NET â¬':>12} {'SLIP%':>7} {'P&L â¬':>10}")
        print(f"  {'-'*6} {'-'*16} {'-'*5} {'-'*10} {'-'*12} {'-'*7} {'-'*10}")
        for t in trades:
            slip_str = f"{t.slippage_pct:+.3f}" if t.slippage_pct is not None else "  n/a "
            pnl_str  = f"{t.realized_pnl_eur:+.2f}" if t.realized_pnl_eur is not None else "    n/a"
            print(
                f"  {t.action:<6} {t.symbol:<16} {t.shares:>5} "
                f"{t.fill_price:>10.4f} {t.net_value_eur:>12.2f} "
                f"{slip_str:>7} {pnl_str:>10}"
            )

    # Slippage advisories
    high_slip = [
        t for t in trades
        if t.slippage_pct is not None and abs(t.slippage_pct) > MAX_SLIPPAGE_WARN_PCT
    ]
    if high_slip:
        print()
        print(f"  â  HIGH SLIPPAGE (> {MAX_SLIPPAGE_WARN_PCT}%):")
        for t in high_slip:
            direction = t.slippage_direction or ""
            print(
                f"    {t.symbol}: {t.slippage_pct:+.4f}% ({direction}) "
                f"fill={t.fill_price:.4f} vs recommended={t.recommended_price:.4f}"
            )

    # Warnings
    if summary.warnings:
        print()
        print(f"  WARNINGS ({len(summary.warnings)}):")
        for w in summary.warnings:
            print(f"    â  {w}")

    # Errors
    if summary.errors:
        print()
        print(f"  ERRORS ({len(summary.errors)}):")
        for e in summary.errors:
            print(f"    â {e}")

    print()
    print("  Next steps:")
    print("    1. Confirm stop-loss orders are live in your broker for all BUY fills.")
    print("    2. Run Script 08 on Friday to update trailing stops.")
    print("    3. Run Script 14 (daily monitor) tomorrow morning.")
    print("=" * 72)
    print()


# ============================================================================
# CLI
# ============================================================================

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="13_log_execution.py",
        description="""
Execution Logger â Script 13 of the Multi-Asset Trend Following Strategy.

Records actual broker fills, updates portfolio_state.json,
appends an immutable entry to trade_ledger.jsonl, and computes
per-trade slippage vs Script 11 recommended prices.

Run AFTER all broker fills for the session are confirmed.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ââ Mode flags ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--batch-file",
        type=Path,
        metavar="PATH",
        help=(
            "Path to a CSV file containing multiple trade fills. "
            "Columns: action, symbol, shares, fill_price, transaction_date (optional), "
            "commission, broker_ref, notes. "
            "Rows with fill_price == 0 are treated as unfilled and skipped. "
            "If transaction_date column is present and non-empty, that date is used per-row; "
            "otherwise --execution-date is used as fallback."
        ),
    )
    mode_group.add_argument(
        "--generate-template",
        action="store_true",
        help=(
            "Generate a pre-populated batch CSV template from the latest "
            "Script 11 recommendations. Does not log any trades. "
            "Requires --rebalancing-ref or an auto-detected file in "
            "reports/rebalancing/."
        ),
    )

    # ââ Single-trade flags (used when --batch-file is not supplied) âââââââââââ
    parser.add_argument(
        "--execution-date",
        metavar="YYYY-MM-DD",
        help=(
            "Date the trade(s) were executed at the broker. "
            "Required for single-trade mode. "
            "Optional for batch mode if CSV has 'transaction_date' column â "
            "used as fallback for rows where transaction_date is empty."
        ),
    )
    parser.add_argument(
        "--action",
        choices=["BUY", "SELL", "buy", "sell"],
        help="Trade direction (single-trade mode only).",
    )
    parser.add_argument(
        "--symbol",
        metavar="TICKER.EXCHANGE",
        help="Instrument ticker with exchange suffix (e.g. AAPL.US).",
    )
    parser.add_argument(
        "--shares",
        type=int,
        metavar="N",
        help="Number of whole shares executed.",
    )
    parser.add_argument(
        "--fill-price",
        type=float,
        metavar="PRICE",
        help="Actual broker fill price in EUR.",
    )
    parser.add_argument(
        "--commission",
        type=float,
        default=0.0,
        metavar="EUR",
        help="Broker commission in EUR (default: 0.0).",
    )
    parser.add_argument(
        "--broker-ref",
        metavar="REF",
        default="",
        help="Optional broker order reference string.",
    )
    parser.add_argument(
        "--notes",
        metavar="TEXT",
        default="",
        help="Optional free-text annotation appended to the ledger entry.",
    )

    # ââ Shared optional flags âââââââââââââââââââââââââââââââââââââââââââââââââ
    parser.add_argument(
        "--rebalancing-ref",
        type=Path,
        metavar="PATH",
        help=(
            "Path to Script 11 recommendations JSON for slippage analysis and "
            "template generation. Auto-detected from reports/rebalancing/ if absent."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Validate and compute results without writing any files. "
            "Useful for verifying a batch CSV before committing."
        ),
    )

    return parser.parse_args()


def validate_cli_args(args: argparse.Namespace) -> None:
    """Enforce mutual requirements between CLI flags."""
    if args.generate_template:
        return  # No further validation needed for template mode

    if args.batch_file is None:
        # Single-trade mode: all core flags are required
        missing = []
        for flag in ("execution_date", "action", "symbol", "shares", "fill_price"):
            if getattr(args, flag) is None:
                missing.append(f"--{flag.replace('_', '-')}")
        if missing:
            print(
                f"ERROR: The following flags are required for single-trade mode: "
                f"{', '.join(missing)}\n"
                "Use --batch-file for multiple trades, or --generate-template "
                "to build a prefilled template."
            )
            sys.exit(1)
    else:
        # Batch mode: execution_date is optional if CSV has transaction_date column
        # (will be validated at parse time if neither is provided)
        pass

    # Validate date format if provided
    if args.execution_date:
        try:
            datetime.strptime(args.execution_date, "%Y-%m-%d")
        except ValueError:
            print(
                f"ERROR: --execution-date '{args.execution_date}' is not a valid "
                "YYYY-MM-DD date."
            )
            sys.exit(1)


# ============================================================================
# MAIN
# ============================================================================

def main() -> int:
    setup_logging()

    logger.info("=" * 70)
    logger.info("EXECUTION LOGGER â Script 13")
    logger.info("Architecture v3.2 (Feb 2026)")
    logger.info("=" * 70)

    args = parse_arguments()
    validate_cli_args(args)

    # ââ Load cross-reference data âââââââââââââââââââââââââââââââââââââââââââââ
    recommendations = load_recommendations(args.rebalancing_ref)

    # ââ Template generation mode ââââââââââââââââââââââââââââââââââââââââââââââ
    if args.generate_template:
        if recommendations is None:
            logger.error(
                "Cannot generate template: no Script 11 recommendations file found.\n"
                "Provide --rebalancing-ref path/to/{YYYY-MM}_recommendations.json"
            )
            return 1

        timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
        tmpl_path  = EXECUTIONS_DIR / f"{timestamp}_batch_template.csv"
        generate_batch_template(recommendations, tmpl_path)
        return 0

    # ââ Collect trades ââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    if args.execution_date:
        logger.info(f"\nExecution date : {args.execution_date}")
    else:
        logger.info("\nExecution date : per-row (from CSV transaction_date column)")
    logger.info(f"Dry run        : {args.dry_run}")

    if args.batch_file:
        logger.info(f"Mode           : BATCH  ({args.batch_file})")
        raw_trades = parse_batch_csv(args.batch_file, args.execution_date)
    else:
        logger.info("Mode           : SINGLE")
        raw_trades = [parse_single_trade(args)]

    if not raw_trades:
        logger.warning("No trades to log (all rows unfilled or empty input).")
        return 0

    # Determine effective session date for reporting (earliest trade date)
    session_date = min(t.execution_date for t in raw_trades)

    # ââ Load current portfolio state ââââââââââââââââââââââââââââââââââââââââââ
    logger.info("\n[1/5] Loading portfolio stateâ¦")
    portfolio = load_portfolio_state()

    # ââ Load existing ledger (for duplicate detection) ââââââââââââââââââââââââ
    logger.info("[2/5] Loading trade ledgerâ¦")
    ledger = load_trade_ledger()

    # ââ Validate all trades âââââââââââââââââââââââââââââââââââââââââââââââââââ
    logger.info("[3/5] Validating tradesâ¦")
    all_errors:   List[str] = []
    all_warnings: List[str] = []
    valid_trades: List[TradeRecord] = []

    for trade in raw_trades:
        errors = validate_trade(trade, portfolio)

        if check_duplicate(trade, ledger):
            errors.append(
                f"DUPLICATE: A matching trade for {trade.symbol} "
                f"({trade.action} {trade.shares} @ {trade.fill_price}) "
                f"already exists in the ledger for {trade.execution_date}."
            )

        if errors:
            for err in errors:
                logger.error(f"  [{trade.symbol}] {err}")
            all_errors.extend([f"[{trade.symbol}] {e}" for e in errors])
        else:
            valid_trades.append(trade)

        # Commission sanity warning (non-blocking)
        gross = trade.fill_price * trade.shares
        if gross > 0 and (trade.commission / gross * 100) > 1.0:
            msg = (
                f"[{trade.symbol}] Commission {trade.commission:.2f} EUR is "
                f"{trade.commission / gross * 100:.2f}% of gross value â "
                "verify this is correct."
            )
            logger.warning(f"  {msg}")
            all_warnings.append(msg)

    if all_errors:
        logger.error(
            f"\n{len(all_errors)} validation error(s) found. "
            "Fix errors in the input and re-run.  No files written."
        )
        return 1

    logger.info(f"  {len(valid_trades)} trade(s) passed validation.")

    # ââ Enrich (compute derived fields and slippage) ââââââââââââââââââââââââââ
    logger.info("[4/5] Enriching trade recordsâ¦")

    # Keep a snapshot of the portfolio BEFORE any mutation (needed for P&L of SELLs)
    portfolio_snapshot = deepcopy(portfolio)

    enriched_trades: List[TradeRecord] = []
    for trade in valid_trades:
        enriched = enrich_trade_record(trade, portfolio_snapshot, recommendations)
        enriched_trades.append(enriched)

    # ââ Apply trades to portfolio state âââââââââââââââââââââââââââââââââââââââ
    logger.info("[5/5] Applying trades to portfolio stateâ¦")
    for trade in enriched_trades:
        portfolio = apply_trade_to_portfolio(portfolio, trade)

    # ââ Build session summary âââââââââââââââââââââââââââââââââââââââââââââââââ
    session_summary = build_session_summary(
        enriched_trades,
        all_warnings,
        all_errors,
        session_date,
    )

    # ââ Persist outputs âââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    save_portfolio_state(portfolio,       dry_run=args.dry_run)
    append_to_ledger(enriched_trades,     dry_run=args.dry_run)
    save_execution_report(
        enriched_trades, session_summary, portfolio,
        session_date, dry_run=args.dry_run,
    )

    # ââ Console summary âââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    print_session_summary(enriched_trades, session_summary, args.dry_run)

    return 0


if __name__ == "__main__":
    sys.exit(main())
