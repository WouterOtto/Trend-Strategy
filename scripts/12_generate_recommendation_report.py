#!/usr/bin/env python3
"""
Script 12: Recommendation Report Generator
===========================================
Converts the monthly rebalancing JSON (Script 11) into a formatted,
human-readable PDF report for review and approval.

Purpose:
    This script is the final "human-in-the-loop" presentation layer.
    It takes the machine-readable JSON produced by Script 11 and renders
    it as a professional PDF document containing:

        - Cover page with status badge and run metadata
        - Circuit breaker status panel
        - Executive summary (action counts + capital deployment)
        - Mandatory exits table (priority 1-3)
        - Rotation exits table (priority 4)
        - New entries table (full order details + technical rationale)
        - Holds table (unrealized P&L + stop status)
        - Capital allocation summary
        - Asset class breakdown
        - Execution checklist
        - Warnings panel
        - Human approval footer

    The report is audit-ready: it contains the full decision trail so
    any action can be traced back to the underlying signals and scores.

    âš   The PDF is for REVIEW only. No trading action is taken by this script.

Inputs:
    reports/rebalancing/{YYYY-MM}_recommendations.json   (Script 11 output)

Outputs:
    reports/rebalancing/{YYYY-MM}_recommendations.pdf
    logs/generate_report_{timestamp}.log

Execution:
    # Auto-detect latest recommendations file
    python scripts/12_generate_recommendation_report.py

    # Specific month
    python scripts/12_generate_recommendation_report.py --month 2026-01

    # Specific input file
    python scripts/12_generate_recommendation_report.py \\
        --input reports/rebalancing/2026-01_recommendations.json

    # Custom output path
    python scripts/12_generate_recommendation_report.py \\
        --month 2026-01 --output /tmp/2026-01_report.pdf

    # Dry-run (validate JSON, no PDF written)
    python scripts/12_generate_recommendation_report.py --month 2026-01 --dry-run

Architecture: v3.2 (Feb 2026)
"""

import os
import sys
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from config.strategies import resolve_strategies, add_strategy_argument, StrategyDef
import json
import logging
import argparse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# ============================================================================
# PATH CONFIGURATION
# ============================================================================

PROJECT_ROOT = Path(__file__).parent.parent
REPORTS_DIR  = PROJECT_ROOT / "reports" / "rebalancing"
LOG_DIR      = PROJECT_ROOT / "logs"

# ============================================================================
# DESIGN TOKENS
# ============================================================================

C_NAVY     = colors.HexColor("#1B2A47")
C_ACCENT   = colors.HexColor("#2E86DE")
C_SUCCESS  = colors.HexColor("#27AE60")
C_DANGER   = colors.HexColor("#E74C3C")
C_ORANGE   = colors.HexColor("#E67E22")
C_HOLD     = colors.HexColor("#7F8C8D")
C_LIGHT_BG = colors.HexColor("#F8F9FA")
C_WHITE    = colors.white
C_BLACK    = colors.black
C_GOLD     = colors.HexColor("#F39C12")

PAGE_W, PAGE_H = A4
MARGIN_H   = 18 * mm
MARGIN_V   = 22 * mm
CONTENT_W  = PAGE_W - 2 * MARGIN_H

# ============================================================================
# LOGGING
# ============================================================================

def setup_logging(month: str) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"generate_report_{month}_{ts}.log"

    logger = logging.getLogger("report_generator")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(logging.Formatter("%(levelname)-8s | %(message)s"))
        logger.addHandler(fh)
        logger.addHandler(ch)

    logger.info(f"Log: {log_file}")
    return logger


logger: logging.Logger = logging.getLogger("report_generator")

# ============================================================================
# FORMATTERS
# ============================================================================

def eur(value: Any, decimals: int = 2) -> str:
    """Format value as EUR string. None -> dash."""
    if value is None:
        return "\u2014"
    try:
        return f"\u20ac{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)


def pct(value: Any, decimals: int = 1) -> str:
    """Format value as percentage string. None -> dash."""
    if value is None:
        return "\u2014"
    try:
        v    = float(value)
        sign = "+" if v > 0 else ""
        return f"{sign}{v:.{decimals}f}%"
    except (TypeError, ValueError):
        return str(value)


def num(value: Any, decimals: int = 2) -> str:
    """Format plain number. None -> dash."""
    if value is None:
        return "\u2014"
    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)


def safe(value: Any, default: str = "\u2014") -> str:
    """Return string or dash."""
    if value is None or value == "" or value == []:
        return default
    return str(value)


def fmt_date(iso: Any) -> str:
    """Format ISO date to DD-Mon-YYYY."""
    if iso is None:
        return "\u2014"
    try:
        dt = datetime.fromisoformat(str(iso).split(".")[0].replace("Z", ""))
        return dt.strftime("%d-%b-%Y")
    except (ValueError, TypeError):
        return str(iso)


# ============================================================================
# STYLE REGISTRY
# ============================================================================

def build_styles() -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    S: Dict[str, ParagraphStyle] = {}

    def style(name, parent_key="Normal", **kwargs):
        S[name] = ParagraphStyle(name, parent=base[parent_key], **kwargs)

    # Cover
    style("cover_title",    fontName="Helvetica-Bold", fontSize=26,
          textColor=C_WHITE, alignment=TA_CENTER, spaceAfter=6)
    style("cover_subtitle", fontName="Helvetica",      fontSize=14,
          textColor=colors.HexColor("#BDC3C7"), alignment=TA_CENTER, spaceAfter=4)
    style("cover_meta",     fontName="Helvetica",      fontSize=10,
          textColor=colors.HexColor("#ECF0F1"), alignment=TA_CENTER, spaceAfter=3)

    # Section headings
    style("section_h1", fontName="Helvetica-Bold", fontSize=12,
          textColor=C_WHITE, alignment=TA_LEFT, spaceAfter=0, spaceBefore=0)
    style("section_h2", fontName="Helvetica-Bold", fontSize=11,
          textColor=C_NAVY, spaceAfter=4, spaceBefore=8)

    # Body
    style("body",        fontName="Helvetica",         fontSize=9,  textColor=C_BLACK,  spaceAfter=3)
    style("body_small",  fontName="Helvetica",         fontSize=7.5, textColor=colors.HexColor("#555555"), spaceAfter=2)
    style("body_italic", fontName="Helvetica-Oblique", fontSize=8,  textColor=colors.HexColor("#777777"), spaceAfter=2)

    # Table cells
    style("cell",        fontName="Helvetica",      fontSize=8, textColor=C_BLACK, alignment=TA_LEFT)
    style("cell_bold",   fontName="Helvetica-Bold", fontSize=8, textColor=C_BLACK, alignment=TA_LEFT)
    style("cell_right",  fontName="Helvetica",      fontSize=8, textColor=C_BLACK, alignment=TA_RIGHT)
    style("cell_center", fontName="Helvetica",      fontSize=8, textColor=C_BLACK, alignment=TA_CENTER)

    # Alerts
    style("alert_danger",  fontName="Helvetica-Bold", fontSize=11, textColor=C_WHITE, alignment=TA_CENTER)
    style("alert_warning", fontName="Helvetica-Bold", fontSize=10, textColor=C_NAVY,  alignment=TA_LEFT)
    style("checklist_item",fontName="Helvetica",      fontSize=9,  textColor=C_BLACK, spaceAfter=5, leftIndent=10)
    style("footer",        fontName="Helvetica-Bold", fontSize=10, textColor=C_WHITE, alignment=TA_CENTER)

    return S


# ============================================================================
# BUILDING BLOCKS
# ============================================================================

def section_header(title: str, S: dict, color=None) -> Table:
    bg = color or C_NAVY
    tbl = Table([[Paragraph(title, S["section_h1"])]], colWidths=[CONTENT_W], rowHeights=[18])
    tbl.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, -1), bg),
        ("LEFTPADDING",  (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING",   (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
    ]))
    return tbl


def mini_kv_table(rows: List[tuple], S: dict, col_widths=None) -> Table:
    """Two-column key-value display table."""
    if col_widths is None:
        col_widths = [CONTENT_W * 0.42, CONTENT_W * 0.58]
    data = [[Paragraph(str(k), S["cell_bold"]), Paragraph(str(v), S["cell"])]
            for k, v in rows]
    tbl = Table(data, colWidths=col_widths)
    tbl.setStyle(TableStyle([
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",   (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW",    (0, 0), (-1, -2), 0.25, colors.HexColor("#EEEEEE")),
    ]))
    return tbl


def data_table(
    headers: List[str],
    rows: List[List[Any]],
    col_widths: List[float],
    S: dict,
    header_bg=None,
    row_colors: Optional[List] = None,
) -> Table:
    """Standard data table with a header row."""
    hdr_bg   = header_bg or C_NAVY
    th_style = ParagraphStyle("th", parent=S["cell_bold"],
                               textColor=C_WHITE, alignment=TA_CENTER, fontSize=7.5)
    header_row = [Paragraph(h, th_style) for h in headers]
    table_data = [header_row] + rows

    style_cmds = [
        ("BACKGROUND",    (0, 0), (-1, 0),  hdr_bg),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 3),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 3),
        ("GRID",          (0, 0), (-1, -1), 0.3, colors.HexColor("#DDDDDD")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [C_WHITE, C_LIGHT_BG]),
    ]
    if row_colors:
        for i, rc in enumerate(row_colors):
            if rc is not None:
                style_cmds.append(("BACKGROUND", (0, i + 1), (-1, i + 1), rc))

    tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle(style_cmds))
    return tbl


def _norm_widths(proportions: List[float]) -> List[float]:
    """Normalize proportions so they sum to CONTENT_W."""
    total = sum(proportions)
    return [p / total * CONTENT_W for p in proportions]


# ============================================================================
# COVER PAGE
# ============================================================================

def build_cover(rec: Dict, S: dict) -> List:
    story = []
    status    = rec.get("status", "")
    month     = rec.get("month", "\u2014")
    r_date    = safe(rec.get("rebalance_date"))
    x_date    = safe(rec.get("execution_date"))
    equity    = rec.get("account_equity", 0)
    gen_at    = safe(rec.get("generated_at"))
    arch      = safe(rec.get("architecture_version", "v3.2"))
    cb        = rec.get("circuit_breakers", {})
    cb_halted = cb.get("halt_all", False)
    cb_entr   = cb.get("halt_entries", False)

    # Title block
    def _hdr_block(text, style_key, bg, pad_top=20, pad_bot=8):
        tbl = Table([[Paragraph(text, S[style_key])]], colWidths=[CONTENT_W])
        tbl.setStyle(TableStyle([
            ("BACKGROUND",   (0,0),(-1,-1), bg),
            ("TOPPADDING",   (0,0),(-1,-1), pad_top),
            ("BOTTOMPADDING",(0,0),(-1,-1), pad_bot),
            ("LEFTPADDING",  (0,0),(-1,-1), 14),
            ("RIGHTPADDING", (0,0),(-1,-1), 14),
        ]))
        return tbl

    story.append(_hdr_block("Multi-Asset Trend Following Strategy",
                             "cover_title", C_NAVY, pad_top=28, pad_bot=8))
    story.append(_hdr_block("Monthly Rebalancing Recommendations",
                             "cover_subtitle", C_NAVY, pad_top=0, pad_bot=10))
    story.append(_hdr_block(f"Period:  {month}",
                             "cover_meta", C_ACCENT, pad_top=6, pad_bot=6))
    story.append(Spacer(1, 14))

    # Status badge
    if cb_halted or "HALTED_ALL" in status:
        badge_bg   = C_DANGER
        badge_text = "STOP  ALL TRADING HALTED  -  CRITICAL CIRCUIT BREAKER TRIGGERED"
    elif cb_entr or "CIRCUIT_BREAKER" in status:
        badge_bg   = C_GOLD
        badge_text = "WARNING  ENTRIES HALTED  -  CIRCUIT BREAKER ACTIVE"
    else:
        badge_bg   = C_SUCCESS
        badge_text = "READY FOR REVIEW"

    badge = Table([[Paragraph(badge_text, S["alert_danger"])]], colWidths=[CONTENT_W])
    badge.setStyle(TableStyle([
        ("BACKGROUND",   (0,0),(-1,-1), badge_bg),
        ("TOPPADDING",   (0,0),(-1,-1), 8),
        ("BOTTOMPADDING",(0,0),(-1,-1), 8),
        ("LEFTPADDING",  (0,0),(-1,-1), 14),
        ("RIGHTPADDING", (0,0),(-1,-1), 14),
    ]))
    story.append(badge)
    story.append(Spacer(1, 16))

    # Key metadata
    meta_rows = [
        ("Rebalance Date",   r_date),
        ("Execution Date",   x_date),
        ("Account Equity",   eur(equity)),
        ("Max Positions",    safe(rec.get("max_positions"))),
        ("Generated At",     gen_at),
        ("Architecture",     arch),
        ("Status",           status),
    ]
    story.append(mini_kv_table(meta_rows, S,
                                col_widths=[CONTENT_W * 0.35, CONTENT_W * 0.65]))
    story.append(Spacer(1, 20))

    # 4-column action count tiles
    exits   = rec.get("exits",   {})
    entries = rec.get("entries", {})
    holds   = rec.get("holds",   {})
    n_mand  = len(exits.get("mandatory", []))
    n_rot   = len(exits.get("rotation",  []))
    n_ent   = len(entries.get("new", []))
    n_hold  = holds.get("total", 0)

    th_sty = ParagraphStyle("th2", parent=S["cell_bold"],
                             textColor=C_WHITE, alignment=TA_CENTER, fontSize=9)
    tv_sty = ParagraphStyle("tv2", parent=S["section_h1"],
                             fontSize=28, alignment=TA_CENTER)

    qw = CONTENT_W / 4
    tiles = Table(
        [
            [Paragraph("MANDATORY EXITS", th_sty), Paragraph("ROTATION EXITS", th_sty),
             Paragraph("NEW ENTRIES",     th_sty), Paragraph("HOLDS",           th_sty)],
            [Paragraph(str(n_mand), tv_sty), Paragraph(str(n_rot), tv_sty),
             Paragraph(str(n_ent),  tv_sty), Paragraph(str(n_hold),tv_sty)],
        ],
        colWidths=[qw, qw, qw, qw],
        rowHeights=[16, 38],
    )
    tiles.setStyle(TableStyle([
        ("BACKGROUND",   (0,0),(0,0), C_DANGER),
        ("BACKGROUND",   (1,0),(1,0), C_ORANGE),
        ("BACKGROUND",   (2,0),(2,0), C_SUCCESS),
        ("BACKGROUND",   (3,0),(3,0), C_HOLD),
        ("BACKGROUND",   (0,1),(0,1), colors.HexColor("#FDEDEC")),
        ("BACKGROUND",   (1,1),(1,1), colors.HexColor("#FEF9E7")),
        ("BACKGROUND",   (2,1),(2,1), colors.HexColor("#EAFAF1")),
        ("BACKGROUND",   (3,1),(3,1), colors.HexColor("#F2F3F4")),
        ("TEXTCOLOR",    (0,1),(0,1), C_DANGER),
        ("TEXTCOLOR",    (1,1),(1,1), C_ORANGE),
        ("TEXTCOLOR",    (2,1),(2,1), C_SUCCESS),
        ("TEXTCOLOR",    (3,1),(3,1), C_HOLD),
        ("VALIGN",       (0,0),(-1,-1), "MIDDLE"),
        ("ALIGN",        (0,0),(-1,-1), "CENTER"),
        ("BOX",          (0,0),(-1,-1), 0.5, colors.HexColor("#CCCCCC")),
        ("INNERGRID",    (0,0),(-1,-1), 0.3, colors.HexColor("#CCCCCC")),
        ("TOPPADDING",   (0,0),(-1,-1), 4),
        ("BOTTOMPADDING",(0,0),(-1,-1), 4),
    ]))
    story.append(tiles)
    story.append(Spacer(1, 14))
    story.append(Paragraph(
        "This report contains system-generated trading recommendations. "
        "ALL recommendations require human review and approval before execution. "
        "The system recommends - the human executes.",
        S["body_italic"],
    ))
    return story


# ============================================================================
# CIRCUIT BREAKER SECTION
# ============================================================================

def build_circuit_breakers(rec: Dict, S: dict) -> List:
    story = []
    cb       = rec.get("circuit_breakers", {})
    breakers = cb.get("breakers", [])
    override = cb.get("override")

    story.append(Spacer(1, 8))
    story.append(section_header("CIRCUIT BREAKER STATUS", S,
                                 color=C_DANGER if breakers else C_SUCCESS))
    story.append(Spacer(1, 6))

    if not breakers:
        story.append(Paragraph(
            "All circuit breakers: CLEAR.  No trading restrictions apply this cycle.",
            S["body"],
        ))
    else:
        for b in breakers:
            sev    = safe(b.get("severity", ""))
            name   = safe(b.get("breaker", "")).replace("_", " ").title()
            detail = safe(b.get("detail"))
            val    = safe(b.get("value"))
            thresh = safe(b.get("threshold"))
            unit   = safe(b.get("unit", ""))
            action = safe(b.get("action", "")).replace("_", " ").title()

            sev_color = (C_DANGER if sev == "CRITICAL"
                         else C_GOLD if sev == "HIGH"
                         else C_ORANGE)

            cb_rows = [
                ("Breaker",   name),
                ("Severity",  sev),
                ("Value",     f"{val} {unit}".strip()),
                ("Threshold", f"{thresh} {unit}".strip()),
                ("Action",    action),
                ("Detail",    detail),
            ]
            cb_tbl = Table(
                [[Paragraph(k, S["cell_bold"]), Paragraph(v, S["cell"])]
                 for k, v in cb_rows],
                colWidths=[CONTENT_W * 0.20, CONTENT_W * 0.80],
            )
            cb_tbl.setStyle(TableStyle([
                ("BACKGROUND",   (0,0),(-1,-1), colors.HexColor("#FEF9E7")),
                ("BACKGROUND",   (0,0),(0,0),   sev_color),
                ("TEXTCOLOR",    (0,0),(0,0),   C_WHITE),
                ("VALIGN",       (0,0),(-1,-1), "TOP"),
                ("TOPPADDING",   (0,0),(-1,-1), 3),
                ("BOTTOMPADDING",(0,0),(-1,-1), 3),
                ("LEFTPADDING",  (0,0),(-1,-1), 5),
                ("RIGHTPADDING", (0,0),(-1,-1), 5),
                ("BOX",          (0,0),(-1,-1), 0.5, sev_color),
                ("LINEBELOW",    (0,0),(-1,-2), 0.3, colors.HexColor("#EEEEEE")),
            ]))
            story.append(cb_tbl)
            story.append(Spacer(1, 6))

    if override:
        story.append(Spacer(1, 4))
        story.append(Paragraph("CIRCUIT BREAKER OVERRIDE ACTIVE", S["alert_warning"]))
        ov_rows = [
            ("Override Active",  "YES"),
            ("Rationale",        safe(override.get("rationale"))),
            ("Overridden At",    fmt_date(override.get("overridden_at"))),
            ("Expires",          safe(override.get("expires_after"))),
        ]
        ov_tbl = Table(
            [[Paragraph(k, S["cell_bold"]), Paragraph(v, S["cell"])]
             for k, v in ov_rows],
            colWidths=[CONTENT_W * 0.20, CONTENT_W * 0.80],
        )
        ov_tbl.setStyle(TableStyle([
            ("BACKGROUND",   (0,0),(-1,-1), colors.HexColor("#EBF5FB")),
            ("TOPPADDING",   (0,0),(-1,-1), 3),
            ("BOTTOMPADDING",(0,0),(-1,-1), 3),
            ("LEFTPADDING",  (0,0),(-1,-1), 5),
            ("RIGHTPADDING", (0,0),(-1,-1), 5),
            ("BOX",          (0,0),(-1,-1), 0.5, C_ACCENT),
            ("LINEBELOW",    (0,0),(-1,-2), 0.3, colors.HexColor("#EEEEEE")),
        ]))
        story.append(ov_tbl)

    return story


# ============================================================================
# EXITS SECTION
# ============================================================================

def build_exits(rec: Dict, S: dict) -> List:
    story = []
    exits     = rec.get("exits", {})
    mandatory = exits.get("mandatory", [])
    rotation  = exits.get("rotation",  [])

    if not mandatory and not rotation:
        story.append(Spacer(1, 8))
        story.append(section_header("EXITS", S, color=C_HOLD))
        story.append(Spacer(1, 6))
        story.append(Paragraph("No exit signals this rebalancing cycle.", S["body"]))
        return story

    # Mandatory exits
    if mandatory:
        story.append(Spacer(1, 8))
        story.append(section_header(
            f"MANDATORY EXITS  ({len(mandatory)})  --  Market Order at Open",
            S, color=C_DANGER))
        story.append(Spacer(1, 4))

        headers = ["Pri", "Symbol", "Reason", "Shares",
                   "Entry Px", "Curr Value", "Unreal P&L", "Curr Stop",
                   "Score", "ADX", "ATR%", "Detail"]
        widths  = _norm_widths([4, 7, 10, 6, 9, 9, 9, 9, 7, 5, 5, 20])

        rows        = []
        row_colors  = []
        for e in mandatory:
            pnl = e.get("unrealized_pnl")
            rc  = (colors.HexColor("#FDEDEC")
                   if pnl is not None and float(pnl or 0) < 0
                   else C_LIGHT_BG)
            row_colors.append(rc)
            rows.append([
                Paragraph(str(safe(e.get("priority"))),              S["cell_center"]),
                Paragraph(safe(e.get("symbol")),                     S["cell_bold"]),
                Paragraph(safe(e.get("reason","")).replace("_"," "), S["cell"]),
                Paragraph(num(e.get("shares"), 0),                   S["cell_right"]),
                Paragraph(eur(e.get("entry_price")),                 S["cell_right"]),
                Paragraph(eur(e.get("current_value")),               S["cell_right"]),
                Paragraph(eur(e.get("unrealized_pnl")),              S["cell_right"]),
                Paragraph(eur(e.get("current_stop")),                S["cell_right"]),
                Paragraph(num(e.get("momentum_score"), 4),           S["cell_right"]),
                Paragraph(num(e.get("adx"), 1),                   S["cell_right"]),
                Paragraph(pct(e.get("atr_pct")),                  S["cell_right"]),
                Paragraph(safe(e.get("detail")),                     S["body_small"]),
            ])
        story.append(data_table(headers, rows, widths, S,
                                 header_bg=C_DANGER, row_colors=row_colors))

    # Rotation exits
    if rotation:
        story.append(Spacer(1, 8))
        story.append(section_header(
            f"ROTATION EXITS  ({len(rotation)})  --  Market Order at Close",
            S, color=C_ORANGE))
        story.append(Spacer(1, 4))

        # Column notes:
        # "Held"      = total shares held in portfolio (= shares to sell, since rotation = full exit)
        # "Sell"      = shares being sold this cycle (always equals Held for rotation exits)
        # "Buy Px"    = entry price per share when position was opened
        # "Curr Px"   = current price per share (from Script 11 current_price field, or
        #               derived as current_value / shares; marked "—*" when stale fallback used)
        # "Total Val" = current total position value
        # "Curr Stop" removed — irrelevant once position is being fully exited
        headers = ["Symbol", "Reason", "Held", "Sell", "Buy Px", "Curr Px",
                   "Total Val", "Unreal P&L", "Entry Date",
                   "Score", "ADX", "ATR%"]
        widths  = _norm_widths([8, 13, 5, 5, 9, 9, 9, 9, 9, 8, 5, 5])

        rows = []
        for e in rotation:
            shares = e.get("shares")
            # Resolve current price per share: prefer explicit Script 11 field, fall back to
            # current_value / shares (may be stale — entry-time value from portfolio_state).
            curr_px       = e.get("current_price")
            price_is_stale = e.get("current_price_is_stale", False)
            if curr_px is None and e.get("current_value") and shares and float(shares) > 0:
                curr_px        = round(float(e["current_value"]) / float(shares), 4)
                price_is_stale = True  # derived from portfolio_state value, not live close

            # When current price couldn't be resolved from a live close, mark it
            curr_px_str = (f"~{eur(curr_px)}" if price_is_stale and curr_px is not None
                           else eur(curr_px))
            rows.append([
                Paragraph(safe(e.get("symbol")),                     S["cell_bold"]),
                Paragraph(safe(e.get("reason","")).replace("_"," "), S["cell"]),
                Paragraph(num(shares, 0),                             S["cell_right"]),  # Held
                Paragraph(num(shares, 0),                             S["cell_right"]),  # Sell (= Held for full rotation exits)
                Paragraph(eur(e.get("entry_price")),                 S["cell_right"]),
                Paragraph(curr_px_str,                                S["cell_right"]),
                Paragraph(eur(e.get("current_value")),               S["cell_right"]),
                Paragraph(eur(e.get("unrealized_pnl")),              S["cell_right"]),
                Paragraph(fmt_date(e.get("entry_date")),             S["cell_center"]),
                Paragraph(num(e.get("momentum_score"), 4),           S["cell_right"]),
                Paragraph(num(e.get("adx"), 1),                   S["cell_right"]),
                Paragraph(pct(e.get("atr_pct")),                  S["cell_right"]),
            ])
        story.append(data_table(headers, rows, widths, S, header_bg=C_ORANGE))
        # Note about stale price indicator
        story.append(Spacer(1, 3))
        story.append(Paragraph(
            "<b>~</b> prefix on Curr Px = live close unavailable; value derived from portfolio "
            "state (may equal Buy Px if position was never re-priced). Verify before settlement.",
            S["body_small"],
        ))

    return story


# ============================================================================
# NEW ENTRIES SECTION
# ============================================================================

def build_entries(rec: Dict, S: dict) -> List:
    story   = []
    entries = rec.get("entries", {}).get("new", [])

    story.append(Spacer(1, 8))
    story.append(section_header(
        f"NEW ENTRIES  ({len(entries)})  --  Limit Order: Close Price + 0.5%",
        S, color=C_SUCCESS))
    story.append(Spacer(1, 4))

    if not entries:
        story.append(Paragraph(
            "No new entries this cycle "
            "(either no candidates pass filters or entries halted by circuit breaker).",
            S["body"],
        ))
        return story

    headers = ["Rank", "Symbol", "Name", "Exchange", "Sector",
               "Shares", "Limit Px", "Pos (EUR)", "Pos %",
               "Init Stop", "Stop Dst%", "ADX", "ATR%", "Score"]
    widths  = _norm_widths([4, 7, 13, 7, 10, 5, 7, 8, 5, 7, 6, 5, 5, 11])

    rows = []
    for e in entries:
        rows.append([
            Paragraph(str(safe(e.get("momentum_rank"))), S["cell_center"]),
            Paragraph(safe(e.get("symbol")),             S["cell_bold"]),
            Paragraph(safe(e.get("name")),               S["body_small"]),
            Paragraph(safe(e.get("exchange")),           S["cell_center"]),
            Paragraph(safe(e.get("sector")),             S["body_small"]),
            Paragraph(num(e.get("shares"), 0),           S["cell_right"]),
            Paragraph(eur(e.get("limit_price")),         S["cell_right"]),
            Paragraph(eur(e.get("position_value_eur")),  S["cell_right"]),
            Paragraph(pct(e.get("position_pct")),        S["cell_right"]),
            Paragraph(eur(e.get("initial_stop")),        S["cell_right"]),
            Paragraph(pct(e.get("stop_distance_pct")),   S["cell_right"]),
            Paragraph(num(e.get("adx"), 1),           S["cell_right"]),
            Paragraph(pct(e.get("atr_pct")),          S["cell_right"]),
            Paragraph(num(e.get("momentum_score"), 4),   S["cell_right"]),
        ])

    story.append(data_table(headers, rows, widths, S, header_bg=C_SUCCESS))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "<b>Order instruction:</b>  Place limit buy orders at Limit Price on the execution "
        "date (first trading day of next month).  Before placement verify: "
        "SMA50 > SMA200,  Close > SMA50,  ADX >= 20.  "
        "Set stop-loss at Initial Stop immediately after fill.",
        S["body_italic"],
    ))
    return story


# ============================================================================
# HOLDS SECTION
# ============================================================================

def build_holds(rec: Dict, S: dict) -> List:
    story     = []
    holds_d   = rec.get("holds", {})
    positions = holds_d.get("positions", [])
    note      = holds_d.get("note", "")

    story.append(Spacer(1, 8))
    story.append(section_header(
        f"HOLDS  ({len(positions)})  --  No Action Required",
        S, color=C_HOLD))
    story.append(Spacer(1, 4))

    if not positions:
        story.append(Paragraph("No positions being held this cycle.", S["body"]))
        return story

    headers = ["Symbol", "Shares", "Entry Px", "Entry Date",
               "Curr Value", "Unreal P&L", "P&L %",
               "Curr Stop", "Stop Type"]
    widths  = _norm_widths([10, 6, 10, 10, 11, 10, 7, 10, 10])
    # pad last
    widths[-1] = CONTENT_W - sum(widths[:-1])

    rows       = []
    row_colors = []
    for h in positions:
        pnl     = h.get("unrealized_pnl")
        pnl_pct = h.get("unrealized_pnl_pct")
        is_pos  = pnl is not None and float(pnl or 0) >= 0
        trailing = h.get("trailing_active", False)
        stop_lbl = "Trailing" if trailing else "Initial"

        row_colors.append(
            colors.HexColor("#EAFAF1") if is_pos else colors.HexColor("#FDEDEC")
        )
        rows.append([
            Paragraph(safe(h.get("symbol")),          S["cell_bold"]),
            Paragraph(num(h.get("shares"), 0),        S["cell_right"]),
            Paragraph(eur(h.get("entry_price")),      S["cell_right"]),
            Paragraph(fmt_date(h.get("entry_date")),  S["cell_center"]),
            Paragraph(eur(h.get("current_value")),    S["cell_right"]),
            Paragraph(eur(pnl),                       S["cell_right"]),
            Paragraph(pct(pnl_pct),                   S["cell_right"]),
            Paragraph(eur(h.get("current_stop")),     S["cell_right"]),
            Paragraph(stop_lbl,                       S["cell_center"]),
        ])

    story.append(data_table(headers, rows, widths, S,
                             header_bg=C_HOLD, row_colors=row_colors))
    if note:
        story.append(Spacer(1, 4))
        story.append(Paragraph(note, S["body_italic"]))

    return story


# ============================================================================
# CAPITAL & ALLOCATION SECTION
# ============================================================================

def build_capital(rec: Dict, S: dict) -> List:
    story = []
    cs    = rec.get("capital_summary", {})
    ab    = rec.get("asset_class_breakdown", {})

    story.append(Spacer(1, 8))
    story.append(section_header("CAPITAL AND ALLOCATION SUMMARY", S, color=C_ACCENT))
    story.append(Spacer(1, 6))

    cs_rows = [
        ("Account Equity",             eur(cs.get("account_equity_eur"))),
        ("Holds Value",                eur(cs.get("holds_value_eur"))),
        ("Capital Freed by Exits",     eur(cs.get("exits_freed_eur_approx")) + "  (approx)"),
        ("Capital Required - Entries", eur(cs.get("entries_required_eur"))),
        ("Total Deployed (after)",     f"{eur(cs.get('total_deployed_after_eur'))}  "
                                        f"({pct(cs.get('total_deployed_after_pct'))})"),
        ("Cash Remaining (approx)",    f"{eur(cs.get('cash_remaining_approx_eur'))}  "
                                        f"({pct(cs.get('cash_remaining_approx_pct'))})"),
    ]
    story.append(mini_kv_table(cs_rows, S,
                                col_widths=[CONTENT_W * 0.38, CONTENT_W * 0.62]))
    if cs.get("note"):
        story.append(Spacer(1, 3))
        story.append(Paragraph(str(cs["note"]), S["body_italic"]))

    if ab:
        story.append(Spacer(1, 10))
        story.append(Paragraph("Asset Class Breakdown  (post-rebalancing)", S["section_h2"]))
        headers = ["Asset Class", "Value (EUR)", "% of Equity", "# Positions"]
        widths  = [CONTENT_W * w for w in [0.35, 0.25, 0.20, 0.20]]
        rows = []
        for ac, info in sorted(ab.items()):
            rows.append([
                Paragraph(ac.upper(),                   S["cell_bold"]),
                Paragraph(eur(info.get("value_eur")),   S["cell_right"]),
                Paragraph(pct(info.get("pct")),         S["cell_right"]),
                Paragraph(str(info.get("count", 0)),    S["cell_center"]),
            ])
        story.append(data_table(headers, rows, widths, S, header_bg=C_ACCENT))

    return story


# ============================================================================
# EXECUTION CHECKLIST + WARNINGS + APPROVAL FOOTER
# ============================================================================

def build_checklist(rec: Dict, S: dict) -> List:
    story     = []
    checklist = rec.get("execution_checklist", [])
    warnings  = rec.get("warnings", [])

    story.append(Spacer(1, 8))
    story.append(section_header("EXECUTION CHECKLIST", S, color=C_NAVY))
    story.append(Spacer(1, 6))

    if checklist:
        for item in checklist:
            story.append(Paragraph(f"  {item}", S["checklist_item"]))
    else:
        story.append(Paragraph("No checklist items available.", S["body"]))

    if warnings:
        story.append(Spacer(1, 10))
        story.append(section_header("WARNINGS", S, color=C_GOLD))
        story.append(Spacer(1, 6))
        for w in warnings:
            story.append(Paragraph(f"WARNING  {w}", S["body"]))
            story.append(Spacer(1, 4))

    # Approval footer
    story.append(Spacer(1, 14))
    approval = Table(
        [[Paragraph(
            "HUMAN APPROVAL REQUIRED BEFORE PLACING ANY ORDERS",
            S["footer"],
        )]],
        colWidths=[CONTENT_W],
    )
    approval.setStyle(TableStyle([
        ("BACKGROUND",   (0,0),(-1,-1), C_DANGER),
        ("TOPPADDING",   (0,0),(-1,-1), 10),
        ("BOTTOMPADDING",(0,0),(-1,-1), 10),
        ("LEFTPADDING",  (0,0),(-1,-1), 14),
        ("RIGHTPADDING", (0,0),(-1,-1), 14),
    ]))
    story.append(approval)
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        f"Execution date: {safe(rec.get('execution_date'))}  |  "
        f"Generated: {safe(rec.get('generated_at'))}  |  "
        f"Architecture: {safe(rec.get('architecture_version', 'v3.2'))}",
        S["body_italic"],
    ))
    return story


# ============================================================================
# PAGE DECORATIONS (header strip + page numbers)
# ============================================================================

def _make_page_decorator(month: str):
    """Return an onPage callback that adds header/footer strips to every page."""
    def _on_page(canvas, doc):
        canvas.saveState()
        w, h = A4
        margin = 18 * mm

        # Top strip
        canvas.setFillColor(C_NAVY)
        canvas.rect(0, h - 8 * mm, w, 8 * mm, fill=1, stroke=0)
        canvas.setFillColor(C_WHITE)
        canvas.setFont("Helvetica", 6.5)
        canvas.drawString(margin, h - 5.5 * mm,
                          f"Multi-Asset Trend Following Strategy  |  {month}  |  CONFIDENTIAL")
        canvas.drawRightString(w - margin, h - 5.5 * mm,
                               "SYSTEM-GENERATED -- REQUIRES HUMAN APPROVAL")

        # Bottom strip
        canvas.setFillColor(C_NAVY)
        canvas.rect(0, 0, w, 7 * mm, fill=1, stroke=0)
        canvas.setFillColor(C_WHITE)
        canvas.setFont("Helvetica", 6.5)
        canvas.drawCentredString(w / 2, 2.5 * mm, f"Page {doc.page}")

        canvas.restoreState()

    return _on_page


# ============================================================================
# FORMAT DETECTION & SCENARIO EXTRACTION
# ============================================================================

def detect_format(rec: Dict) -> str:
    """
    Detect the JSON format produced by Script 11.

    Returns:
        'halted':  Circuit-breaker HALT_ALL state — no scenario or trade data present.
                   Script 11 saves a minimal JSON with only metadata and breaker info.
        'multi':   Normal multi-scenario format (Script 11 v3.2+) — scenario_1_...
                   scenario_2_... scenario_3_... keys present.
        'single':  Legacy single-scenario format — exits/entries/holds at top level.
    """
    status = rec.get("status", "")
    cb     = rec.get("circuit_breakers", {})

    # A HALT_ALL JSON has no actionable trade data; detect it first so we never
    # attempt format-specific parsing on an empty/minimal record.
    is_halted = (
        cb.get("halt_all", False)
        or "HALTED_ALL" in status
        or "HALTED" in status
    )
    has_scenarios     = "scenarios" in rec
    has_scenario_keys = any(k.startswith("scenario_") for k in rec.keys())
    has_trade_keys    = "exits" in rec and "entries" in rec and "holds" in rec

    if is_halted and not has_scenarios and not has_scenario_keys and not has_trade_keys:
        return "halted"

    # Check for multi-scenario format (Script 11 v3.2+)
    if has_scenarios or has_scenario_keys:
        return "multi"

    # Check for single-scenario format (old Script 11)
    if has_trade_keys:
        return "single"

    raise ValueError(
        "Unknown JSON format. Expected either:\n"
        "  - Halted: status contains HALTED and no trade data\n"
        "  - Multi-scenario: 'scenario_1_...', 'scenario_2_...', 'scenario_3_...' keys\n"
        "  - Single-scenario: 'exits', 'entries', 'holds' keys"
    )


def extract_scenario_data(rec: Dict, scenario_number: int) -> Dict:
    """
    Extract a specific scenario from multi-scenario JSON and convert to single-scenario format.
    
    Args:
        rec: Full multi-scenario recommendations dict
        scenario_number: 1, 2, or 3
    
    Returns:
        Dict in single-scenario format (compatible with existing build functions)
    """
    # Handle both nested and flat structures
    if 'scenarios' in rec:
        scenarios = rec['scenarios']
        scenario_map = {
            1: 'scenario1_pure_momentum',
            2: 'scenario2_force_diversity',
            3: 'scenario3_balanced'
        }
    else:
        # Flat structure - scenarios at top level (Script 11 v3.2 default)
        scenarios = rec
        scenario_map = {
            1: 'scenario_1_pure_momentum',
            2: 'scenario_2_force_diversity',
            3: 'scenario_3_balanced'
        }
    
    scenario_key = scenario_map.get(scenario_number)
    if not scenario_key or scenario_key not in scenarios:
        raise ValueError(f"Scenario {scenario_number} not found in JSON")
    
    scenario = scenarios[scenario_key]
    
    # Convert to single-scenario format (compatible with existing functions)
    return {
        'month': rec.get('month'),
        'rebalance_date': rec.get('rebalance_date'),
        'execution_date': rec.get('execution_date'),
        'account_equity': rec.get('account_equity'),
        'max_positions': rec.get('max_positions'),
        'generated_at': rec.get('generated_at'),
        'architecture_version': rec.get('architecture_version'),
        'status': rec.get('status'),
        'circuit_breakers': rec.get('circuit_breakers', {}),
        
        # Exits and holds (empty for new portfolio)
        'exits': scenario.get('exits', {'mandatory': [], 'rotation': []}),
        'holds': scenario.get('holds', {'positions': [], 'total': 0}),
        
        # Entries from scenario
        'entries': scenario.get('entries', {'new': [], 'total': 0}),
        
        # Capital summary
        'capital_summary': scenario.get('capital_summary', {}),
        
        # Asset breakdown
        'asset_breakdown': scenario.get('asset_breakdown', {}),
        
        # Warnings
        'warnings': scenario.get('warnings', []),
        
        # Execution checklist
        'execution_checklist': rec.get('execution_checklist', []),
        
        # Scenario metadata (for display)
        '_scenario_number': scenario_number,
        '_scenario_name': scenario.get('name', f'Scenario {scenario_number}'),
        '_scenario_philosophy': scenario.get('philosophy', ''),
        '_scenario_description': scenario.get('description', ''),
    }


def build_comparison_section(rec: Dict, S: Dict) -> List:
    """
    Build comparison section showing all three scenarios side-by-side.
    
    Args:
        rec: Multi-scenario recommendations dict
        S: Style dictionary
    
    Returns:
        List of ReportLab flowables
    """
    story = []
    
    # Section header
    story.append(Spacer(1, 12 * mm))
    story.append(Paragraph("THREE-SCENARIO COMPARISON", S["section_h1"]))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        "Review all three scenarios below. Each scenario represents a different "
        "balance between momentum strength and diversification. Choose ONE scenario "
        "based on your risk/return preferences, then review the detailed execution "
        "plan for your chosen scenario in the following sections.",
        S["body"]
    ))
    story.append(Spacer(1, 5 * mm))
    
    # Handle both nested and flat structures
    if 'scenarios' in rec:
        scenarios = rec['scenarios']
        scenario_keys = {
            's1': 'scenario1_pure_momentum',
            's2': 'scenario2_force_diversity',
            's3': 'scenario3_balanced'
        }
    else:
        scenarios = rec
        scenario_keys = {
            's1': 'scenario_1_pure_momentum',
            's2': 'scenario_2_force_diversity',
            's3': 'scenario_3_balanced'
        }
    
    # Extract scenarios
    s1 = scenarios.get(scenario_keys['s1'], {})
    s2 = scenarios.get(scenario_keys['s2'], {})
    s3 = scenarios.get(scenario_keys['s3'], {})
    
    # Build comparison table
    table_data = [
        ["Metric", "Scenario 1\nPure Momentum", "Scenario 2\nForce Diversity", "Scenario 3\nBalanced"]
    ]
    
    # Philosophy
    table_data.append([
        "Philosophy",
        s1.get('philosophy', '—')[:50],
        s2.get('philosophy', '—')[:50],
        s3.get('philosophy', '—')[:50]
    ])
    
    # Candidate pool
    table_data.append([
        "Candidate Pool",
        s1.get('candidate_pool_display', '—'),
        s2.get('candidate_pool_display', '—'),
        s3.get('candidate_pool_display', '—')
    ])
    
    # Number of positions
    n1 = len(s1.get('entries', {}).get('new', []))
    n2 = len(s2.get('entries', {}).get('new', []))
    n3 = len(s3.get('entries', {}).get('new', []))
    table_data.append(["New Positions", str(n1), str(n2), str(n3)])
    
    # Calculate values from entries (asset_breakdown.value_eur may be null)
    def calc_values_by_class(scenario):
        entries = scenario.get('entries', {}).get('new', [])
        values = {}
        for e in entries:
            ac = e.get('asset_class', 'stock')
            values[ac] = values.get(ac, 0) + e.get('position_value_eur', 0)
        return values
    
    vals1 = calc_values_by_class(s1)
    vals2 = calc_values_by_class(s2)
    vals3 = calc_values_by_class(s3)
    
    # Get percentages from asset_breakdown
    ab1 = s1.get('asset_breakdown', {})
    ab2 = s2.get('asset_breakdown', {})
    ab3 = s3.get('asset_breakdown', {})
    
    table_data.append(["", "", "", ""])  # Spacer
    table_data.append(["ASSET ALLOCATION", "", "", ""])
    
    for ac in ['etf', 'stock', 'crypto']:
        ac_label = ac.upper()
        val1 = vals1.get(ac, 0)
        val2 = vals2.get(ac, 0)
        val3 = vals3.get(ac, 0)
        pct1 = ab1.get(ac, {}).get('pct', 0)
        pct2 = ab2.get(ac, {}).get('pct', 0)
        pct3 = ab3.get(ac, {}).get('pct', 0)
        
        table_data.append([
            ac_label,
            f"€{val1:,.0f} ({pct1:.1f}%)",
            f"€{val2:,.0f} ({pct2:.1f}%)",
            f"€{val3:,.0f} ({pct3:.1f}%)"
        ])
    
    # Total deployed
    table_data.append(["", "", "", ""])
    equity = rec.get('account_equity', 15000)
    total1 = sum(vals1.values())
    total2 = sum(vals2.values())
    total3 = sum(vals3.values())
    
    table_data.append([
        "TOTAL DEPLOYED",
        f"€{total1:,.0f} ({total1/equity*100:.1f}%)",
        f"€{total2:,.0f} ({total2/equity*100:.1f}%)",
        f"€{total3:,.0f} ({total3/equity*100:.1f}%)"
    ])
    
    # Create table
    col_widths = [38*mm, 44*mm, 44*mm, 44*mm]
    t = Table(table_data, colWidths=col_widths)
    t.setStyle(TableStyle([
        # Header row
        ('BACKGROUND', (0, 0), (-1, 0), C_NAVY),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        
        # Data rows
        ('FONTNAME', (0, 1), (0, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 1), (-1, -1), 8),
        ('ALIGN', (0, 1), (0, -1), 'LEFT'),
        ('ALIGN', (1, 1), (-1, -1), 'CENTER'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, C_LIGHT_BG]),
    ]))
    
    story.append(t)
    story.append(Spacer(1, 8 * mm))
    
    # Key differences summary
    story.append(Paragraph("<b>Key Differences:</b>", S["body"]))
    story.append(Spacer(1, 2 * mm))
    
    diff_points = []
    if total1 != total2 or total1 != total3:
        max_total = max(total1, total2, total3)
        min_total = min(total1, total2, total3)
        diff_pct = ((max_total - min_total) / equity * 100) if equity > 0 else 0
        diff_points.append(f"• <b>Capital deployment</b> varies by {diff_pct:.1f}pp across scenarios")
    
    etf_vals = [vals1.get('etf', 0), vals2.get('etf', 0), vals3.get('etf', 0)]
    if any(v > 0 for v in etf_vals):
        max_etf_pct = max(ab1.get('etf', {}).get('pct', 0), 
                         ab2.get('etf', {}).get('pct', 0),
                         ab3.get('etf', {}).get('pct', 0))
        if max_etf_pct > 0:
            diff_points.append(f"• Scenario 2 provides <b>highest ETF exposure</b> ({max_etf_pct:.1f}%) for geographic diversification")
    
    if n1 != n2 or n1 != n3:
        diff_points.append(f"• <b>Position count</b> varies from {min(n1,n2,n3)} to {max(n1,n2,n3)} positions")
    
    if not diff_points:
        diff_points.append("• All scenarios currently similar due to market conditions")
    
    for point in diff_points:
        story.append(Paragraph(point, S["body"]))
    
    story.append(Spacer(1, 5 * mm))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.grey, spaceBefore=3*mm, spaceAfter=3*mm))
    
    return story


def _collect_ranked_candidates(rec: Dict) -> List[Dict]:
    """
    Collect all ranked candidates for the Top-50 table.

    Priority order:
      1.  rec['ranked_candidates']  – flat list produced by Script 11 v3.3+
      2.  rec['universe']           – alternative key name
      3.  Merge entries from all three scenarios (dedup by symbol,
          keep the copy with the highest momentum_score)

    In all cases, every candidate is tagged with '_in_scenarios' (a set of
    "S1"/"S2"/"S3" labels) so the Top-50 table can colour-code membership.

    Returns a list sorted by momentum_score descending (up to 50 items).
    """
    # Helper: build per-scenario symbol lookup from the JSON
    def _build_scenario_membership(rec: Dict) -> Dict[str, set]:
        """Return {symbol: {S1, S2, S3}} for all scenario entries."""
        if "scenarios" in rec:
            scenarios  = rec["scenarios"]
            s_keys     = ["scenario1_pure_momentum",
                          "scenario2_force_diversity",
                          "scenario3_balanced"]
        else:
            scenarios  = rec
            s_keys     = ["scenario_1_pure_momentum",
                          "scenario_2_force_diversity",
                          "scenario_3_balanced"]

        labels     = ["S1", "S2", "S3"]
        membership: Dict[str, set] = {}
        for sk, label in zip(s_keys, labels):
            scenario = scenarios.get(sk, {})
            for entry in scenario.get("entries", {}).get("new", []):
                sym = entry.get("symbol", "")
                if sym:
                    membership.setdefault(sym, set()).add(label)
        return membership

    membership = _build_scenario_membership(rec)

    # --- Source 1 & 2: explicit ranked list ---
    for key in ("ranked_candidates", "universe", "candidate_universe"):
        raw = rec.get(key)
        if raw and isinstance(raw, list) and len(raw) > 0:
            # Tag each candidate with scenario membership
            result = []
            for c in sorted(raw, key=lambda x: x.get("momentum_score", 0), reverse=True)[:50]:
                sym = c.get("symbol", "")
                tagged = dict(c)
                tagged["_in_scenarios"] = membership.get(sym, set())
                result.append(tagged)
            return result

    # --- Source 3: merge scenario entries ---
    if "scenarios" in rec:
        scenarios = rec["scenarios"]
        scenario_keys = [
            "scenario1_pure_momentum",
            "scenario2_force_diversity",
            "scenario3_balanced",
        ]
    else:
        scenarios = rec
        scenario_keys = [
            "scenario_1_pure_momentum",
            "scenario_2_force_diversity",
            "scenario_3_balanced",
        ]

    scenario_labels = ["S1", "S2", "S3"]
    merged: Dict[str, Dict] = {}

    for sk, label in zip(scenario_keys, scenario_labels):
        scenario = scenarios.get(sk, {})
        for entry in scenario.get("entries", {}).get("new", []):
            sym = entry.get("symbol", "")
            if not sym:
                continue
            if sym not in merged:
                merged[sym] = dict(entry)
                merged[sym]["_in_scenarios"] = set()
            else:
                # keep highest-score copy of shared fields
                if entry.get("momentum_score", 0) > merged[sym].get("momentum_score", 0):
                    merged[sym].update(entry)
                    merged[sym]["_in_scenarios"] = merged[sym].get("_in_scenarios", set())
            merged[sym]["_in_scenarios"].add(label)

    # Add scenario membership for symbols that only appeared once
    for sym, data in merged.items():
        if "_in_scenarios" not in data:
            data["_in_scenarios"] = set()

    # Tag which scenarios each symbol is in
    for sk, label in zip(scenario_keys, scenario_labels):
        scenario = scenarios.get(sk, {})
        syms_in = {e.get("symbol") for e in scenario.get("entries", {}).get("new", [])}
        for sym in merged:
            if sym in syms_in:
                merged[sym]["_in_scenarios"].add(label)

    result = sorted(merged.values(), key=lambda x: x.get("momentum_score", 0), reverse=True)
    return result[:50]


def build_top50_table(rec: Dict, S: dict) -> List:
    """
    Build a full-width, landscape-style Top-50 Candidates table.

    Columns
    -------
    # | Symbol | Name | Class | S1 | S2 | S3 | Score | ADX | ATR% |
    12M% | 6M% | 3M% | Entry € | Stop € | Stop% | Shares | Value €

    Color coding
    ------------
    • Rows that appear in ALL THREE scenarios  → light green background
    • Rows that appear in exactly TWO scenarios → light blue
    • Rows in only ONE scenario                 → white / light-grey alternating
    • Score column value ≥ top-quartile         → bold
    """
    story: List = []

    story.append(PageBreak())
    story.append(Spacer(1, 4))
    story.append(section_header(
        "TOP-50 CANDIDATE UNIVERSE  —  Full Metrics Decision Table",
        S, color=C_NAVY,
    ))
    story.append(Spacer(1, 5))
    story.append(Paragraph(
        "Every ranked candidate that cleared all strategy filters is shown below. "
        "Checkmarks (✓) indicate in which scenario(s) a symbol was selected. "
        "Use this table to make your final BUY decisions — override any scenario's list "
        "by choosing from this pool.  "
        "<b>Green rows</b> = selected in all three scenarios. "
        "<b>Blue rows</b> = selected in two scenarios. "
        "White/grey rows = selected in one scenario only.",
        S["body_small"],
    ))
    story.append(Spacer(1, 5))

    candidates = _collect_ranked_candidates(rec)

    if not candidates:
        story.append(Paragraph(
            "No ranked candidates found in the JSON.  "
            "Ensure Script 11 was run with --export-universe or upgrade to v3.3+.",
            S["body"],
        ))
        return story

    # ------------------------------------------------------------------ headers
    # We use two header rows: group row + column row
    # Column widths (must sum ≈ CONTENT_W = 174 mm)
    # #(5) Sym(17) Name(24) Cls(8) S1(7) S2(7) S3(7) Scr(12) ADX(9) ATR%(9)
    # 12M(10) 6M(9) 3M(9) Entry€(13) Stop€(13) Stp%(8) Shs(10) Val€(13)
    COL_W = [5, 17, 24, 8, 7, 7, 7, 12, 9, 9, 10, 9, 9, 13, 13, 8, 10, 13]
    # Normalize to CONTENT_W
    total = sum(COL_W)
    col_widths = [w / total * CONTENT_W for w in COL_W]

    th = ParagraphStyle(
        "th50",
        parent=S["cell_bold"],
        textColor=C_WHITE,
        alignment=TA_CENTER,
        fontSize=6.5,
    )
    td       = ParagraphStyle("td50",  parent=S["cell"],       fontSize=6.5, alignment=TA_LEFT)
    td_r     = ParagraphStyle("td50r", parent=S["cell_right"], fontSize=6.5, alignment=TA_RIGHT)
    td_c     = ParagraphStyle("td50c", parent=S["cell_center"],fontSize=6.5, alignment=TA_CENTER)
    td_bold  = ParagraphStyle("td50b", parent=S["cell_bold"],  fontSize=6.5, alignment=TA_RIGHT)

    # Group header
    grp_row = [
        Paragraph("",            th),   # #
        Paragraph("",            th),   # Symbol
        Paragraph("",            th),   # Name
        Paragraph("",            th),   # Class
        Paragraph("SCENARIOS",   th),   # S1
        Paragraph("",            th),   # S2
        Paragraph("",            th),   # S3
        Paragraph("MOMENTUM",    th),   # Score
        Paragraph("",            th),   # ADX
        Paragraph("",            th),   # ATR%
        Paragraph("RETURNS",     th),   # 12M
        Paragraph("",            th),   # 6M
        Paragraph("",            th),   # 3M
        Paragraph("ORDER",       th),   # Entry€
        Paragraph("",            th),   # Stop€
        Paragraph("",            th),   # Stp%
        Paragraph("",            th),   # Shares
        Paragraph("",            th),   # Val€
    ]

    col_row = [
        Paragraph("#",       th),
        Paragraph("Symbol",  th),
        Paragraph("Name",    th),
        Paragraph("Class",   th),
        Paragraph("S1",      th),
        Paragraph("S2",      th),
        Paragraph("S3",      th),
        Paragraph("Score",   th),
        Paragraph("ADX",     th),
        Paragraph("ATR%",    th),
        Paragraph("12M%",    th),
        Paragraph("6M%",     th),
        Paragraph("3M%",     th),
        Paragraph("Entry €", th),
        Paragraph("Stop €",  th),
        Paragraph("Stp%",    th),
        Paragraph("Shares",  th),
        Paragraph("Val €",   th),
    ]

    table_data = [grp_row, col_row]

    # ------------------------------------------------------------------ data rows
    scores = [c.get("momentum_score", 0) for c in candidates]
    score_p75 = sorted(scores)[int(len(scores) * 0.75)] if scores else 0

    row_colors: List = [None, None]  # for the two header rows

    for rank, c in enumerate(candidates, start=1):
        in_s = c.get("_in_scenarios", set())
        if not isinstance(in_s, set):
            in_s = set(in_s) if in_s else set()

        n_scenarios = len(in_s)

        # Background tint
        if n_scenarios >= 3:
            row_bg = colors.HexColor("#D5F5E3")   # green
        elif n_scenarios == 2:
            row_bg = colors.HexColor("#D6EAF8")   # blue
        else:
            row_bg = None                          # alternating handled by ROWBACKGROUNDS

        row_colors.append(row_bg)

        score_val = c.get("momentum_score", 0)
        score_str = f"{score_val:.2f}" if score_val else "—"
        score_p   = td_bold if score_val and score_val >= score_p75 else td_r

        # Tick mark helper
        def tick(label: str) -> str:
            return "✓" if label in in_s else "·"

        # Return fields – try multiple key variants
        # Script 11 exports: return_6m (roc_120d), return_3m (roc_60d), return_1m (roc_20d)
        ret12 = (c.get("return_12m") or c.get("return_12m_pct") or c.get("mom_12m")
                 or c.get("return_1m") or c.get("roc_20d"))
        ret6  = (c.get("return_6m")  or c.get("return_6m_pct")  or c.get("mom_6m")
                 or c.get("roc_120d"))
        ret3  = (c.get("return_3m")  or c.get("return_3m_pct")  or c.get("mom_3m")
                 or c.get("roc_60d"))

        entry_px  = c.get("limit_price") or c.get("entry_price") or c.get("close")
        stop_px   = c.get("initial_stop") or c.get("stop_loss")
        stop_dst  = c.get("stop_distance_pct")
        shares    = c.get("shares")
        val_eur   = c.get("position_value_eur")
        # Prefix estimated (fallback) values with "~" so the reader knows they
        # are ATR-derived approximations rather than Script 08/09 exact outputs.
        is_estimated = c.get("sizing_estimated", False)
        est_pfx = "~" if is_estimated else ""

        # Truncate long name
        name_str = safe(c.get("name", ""))
        if len(name_str) > 22:
            name_str = name_str[:20] + "…"

        sector_str = safe(c.get("sector",""))
        if len(sector_str) > 12:
            sector_str = sector_str[:11] + "…"

        table_data.append([
            Paragraph(str(rank),               td_c),
            Paragraph(safe(c.get("symbol")),   td_bold if n_scenarios >= 2 else td),
            Paragraph(name_str,                td),
            Paragraph(safe(c.get("asset_class","")).upper()[:4], td_c),
            Paragraph(tick("S1"),              td_c),
            Paragraph(tick("S2"),              td_c),
            Paragraph(tick("S3"),              td_c),
            Paragraph(score_str,               score_p),
            Paragraph(num(c.get("adx"), 1), td_r),
            Paragraph(pct(c.get("atr_pct")),td_r),
            Paragraph(pct(ret12),              td_r),
            Paragraph(pct(ret6),               td_r),
            Paragraph(pct(ret3),               td_r),
            Paragraph(eur(entry_px),                              td_r),
            Paragraph((est_pfx + eur(stop_px)) if stop_px else "—",    td_r),
            Paragraph((est_pfx + pct(stop_dst)) if stop_dst else "—",  td_r),
            Paragraph((est_pfx + num(shares, 0)) if shares else "—",   td_r),
            Paragraph((est_pfx + eur(val_eur))  if val_eur else "—",   td_r),
        ])

    # ------------------------------------------------------------------ assemble table
    style_cmds = [
        # Group header (row 0) spanning
        ("BACKGROUND",    (0, 0), (-1, 0),  C_ACCENT),
        ("SPAN",          (4, 0), (6, 0)),            # SCENARIOS
        ("SPAN",          (7, 0), (9, 0)),            # MOMENTUM
        ("SPAN",          (10,0), (12,0)),            # RETURNS
        ("SPAN",          (13,0), (17,0)),            # ORDER
        # Column header (row 1)
        ("BACKGROUND",    (0, 1), (-1, 1),  C_NAVY),
        # All rows
        ("FONTSIZE",      (0, 0), (-1, -1), 6.5),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING",   (0, 0), (-1, -1), 2),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 2),
        ("GRID",          (0, 0), (-1, -1), 0.25, colors.HexColor("#CCCCCC")),
        # Default alternating rows (overridden per-row below)
        ("ROWBACKGROUNDS",(0, 2), (-1, -1), [C_WHITE, C_LIGHT_BG]),
    ]

    # Apply per-row backgrounds for 2-scenario (blue) and 3-scenario (green) rows
    for i, bg in enumerate(row_colors):
        if bg is not None:
            style_cmds.append(("BACKGROUND", (0, i), (-1, i), bg))

    tbl = Table(table_data, colWidths=col_widths, repeatRows=2)
    tbl.setStyle(TableStyle(style_cmds))

    story.append(tbl)
    story.append(Spacer(1, 5))
    story.append(Paragraph(
        "Scores in <b>bold</b> are in the top quartile. "
        "Entry € = Limit price (last close + 0.5%). "
        "Stop € = Initial ATR-based stop-loss. "
        "Stp% = distance from entry to stop. "
        "Shares & Val € = position size at full allocation. "
        "<b>Values prefixed with ~ are ATR-derived estimates</b> (Script 08/09 data absent; "
        "treat as indicative only — verify before trading). "
        "Returns are raw price returns (dividends excluded).",
        S["body_small"],
    ))
    return story


def build_multi_scenario_pdf(rec: Dict, output_path: Path, strategy_name: str = '') -> None:
    """
    Build comprehensive PDF with comparison + full execution details for all 3 scenarios.
    
    This is the main PDF generation function for multi-scenario JSON (Script 11 v3.2+).
    Generates a single PDF containing:
        - Part 1: Three-scenario comparison (decision support)
        - Part 2: Scenario 1 full execution details
        - Part 3: Scenario 2 full execution details  
        - Part 4: Scenario 3 full execution details
        - Part 5: Final decision checklist
    
    Args:
        rec: Multi-scenario recommendations dict from Script 11
        output_path: Path for output PDF
    """
    logger.info("Building multi-scenario comprehensive PDF...")
    
    month = rec.get("month", "unknown")
    S = build_styles()
    
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        topMargin=MARGIN_V + 8 * mm,  # Extra space for header strip
        bottomMargin=MARGIN_V + 7 * mm,  # Extra space for footer strip
        leftMargin=MARGIN_H,
        rightMargin=MARGIN_H,
        title=f"Monthly Rebalancing Recommendations - {month}",
        author="Trend Following Strategy System v3.2",
    )
    
    # Page decorator (header/footer)
    on_page = _make_page_decorator(month)
    
    story = []
    # ── Strategy identification header ──────────────────────────
    if strategy_name:
        from config.strategies import StrategyRegistry
        try:
            _reg = StrategyRegistry()
            _s   = _reg.get(strategy_name)
            _lbl = f"{_s.label}  ·  {'LIVE' if _s.deployed else 'PAPER TRADING'}"
        except Exception:
            _lbl = strategy_name.upper()
        _strat_style = ParagraphStyle('StratBadge', fontSize=11, textColor=colors.HexColor('#1a3a5c'), spaceAfter=4, spaceBefore=0, fontName='Helvetica-Bold')
        _strat_para  = Paragraph(f'Strategy: {_lbl}', _strat_style)

    
    # ========================================================================
    # PART 1: COVER & COMPARISON
    # ========================================================================
    
    # Cover page
    story.append(Spacer(1, 25 * mm))
    story.append(Paragraph(
        "MONTHLY REBALANCING RECOMMENDATIONS",
        S["cover_title"]
    ))
    story.append(Spacer(1, 5 * mm))
    story.append(Paragraph(
        "Multi-Scenario Analysis & Execution Plan",
        S["section_h1"]
    ))
    story.append(Spacer(1, 15 * mm))
    
    # Metadata
    meta_data = [
        ["Period:", rec.get("month", "—")],
        ["Rebalance Date:", rec.get("rebalance_date", "—")],
        ["Execution Date:", rec.get("execution_date", "—")],
        ["Account Equity:", f"€{rec.get('account_equity', 0):,.2f}"],
        ["Max Positions:", str(rec.get("max_positions", "—"))],
        ["Generated:", rec.get("generated_at", "—")[:19]],
        ["Architecture:", rec.get("architecture_version", "v3.2")],
    ]
    
    for label, value in meta_data:
        story.append(Paragraph(f"<b>{label}</b> {value}", S["body"]))
    
    story.append(Spacer(1, 15 * mm))
    
    # Status badge
    status = rec.get("status", "UNKNOWN")
    status_color = C_SUCCESS if status == "READY_FOR_REVIEW" else C_ORANGE
    story.append(Paragraph(
        f'<para alignment="center" backColor="{status_color}" textColor="white" '
        f'leftIndent="10" rightIndent="10" spaceBefore="5" spaceAfter="5">'
        f'<b>{status}</b></para>',
        S["body"]
    ))
    
    story.append(Spacer(1, 10 * mm))
    story.append(Paragraph(
        "<b>⚠ HUMAN APPROVAL REQUIRED</b>",
        S["alert_warning"]
    ))
    story.append(Paragraph(
        "This report contains system-generated recommendations. All recommendations "
        "require human review and approval before execution. The system recommends — "
        "the human executes.",
        S["body"]
    ))
    
    story.append(PageBreak())
    
    # Comparison section
    story.extend(build_comparison_section(rec, S))
    story.append(PageBreak())
    
    # ========================================================================
    # PART 2-4: FULL EXECUTION DETAILS FOR EACH SCENARIO
    # ========================================================================
    
    for scenario_num in [1, 2, 3]:
        logger.info(f"  Building Scenario {scenario_num} execution details...")
        
        # Extract scenario data in single-scenario format
        scenario_data = extract_scenario_data(rec, scenario_num)
        
        # Scenario header page
        story.append(Spacer(1, 20 * mm))
        story.append(Paragraph(
            f"SCENARIO {scenario_num}: {scenario_data['_scenario_name'].upper()}",
            S["cover_title"]
        ))
        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph(
            f"<i>{scenario_data['_scenario_philosophy']}</i>",
            S["body"]
        ))
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph(
            scenario_data['_scenario_description'],
            S["body"]
        ))
        story.append(Spacer(1, 8 * mm))
        story.append(HRFlowable(width="100%", thickness=2, color=C_ACCENT))
        story.append(Spacer(1, 8 * mm))
        
        # Circuit breakers (same for all scenarios)
        if scenario_num == 1:
            cbs = scenario_data.get("circuit_breakers", {})
            cb_list = cbs.get("breakers", [])
            
            story.append(Paragraph("CIRCUIT BREAKER STATUS", S["section_h1"]))
            story.append(Spacer(1, 3 * mm))
            
            if not cb_list or all(not b.get("triggered") for b in cb_list):
                story.append(Paragraph(
                    '<para backColor="#27AE60" textColor="white" leftIndent="5" rightIndent="5">'
                    '<b>✓ ALL CLEAR</b> — No circuit breakers triggered. Trading unrestricted.</para>',
                    S["body"]
                ))
            else:
                for cb in cb_list:
                    if cb.get("triggered"):
                        story.append(Paragraph(
                            f'<para backColor="#E74C3C" textColor="white" leftIndent="5" rightIndent="5">'
                            f'<b>⚠ {cb.get("name", "UNKNOWN")}</b>: {cb.get("message", "")}</para>',
                            S["body"]
                        ))
            
            story.append(Spacer(1, 6 * mm))
        
        # Build execution sections using existing functions (they expect single-scenario format)
        # These functions are already defined in the original Script 12
        
        # Exits — delegate to build_exits() which renders the full canonical table
        # with correct columns: Sell All, Buy Px, Curr Px, Total Val, Unreal P&L,
        # Curr Stop, Entry Date, Score, ADX, ATR% (Issues 1 & 2).
        story += build_exits(scenario_data, S)
        
        # Extract exit lists here so they are in scope for the checklist below
        _exits    = scenario_data.get("exits", {})
        mandatory = _exits.get("mandatory", [])
        rotation  = _exits.get("rotation",  [])

        # New Entries
        entries = scenario_data.get("entries", {}).get("new", [])
        
        story.append(Paragraph(
            f"NEW ENTRIES ({len(entries)}) — Limit Order: Close Price + 0.5%",
            S["section_h1"]
        ))
        story.append(Spacer(1, 3 * mm))
        
        if entries:
            entry_data = [["#", "Symbol", "Name", "Class", "Shares", "Entry €", "Value", "Stop €", "ADX", "ATR%", "Score"]]
            for i, entry in enumerate(entries, 1):
                entry_data.append([
                    str(i),
                    entry.get("symbol", "")[:12],
                    entry.get("name", "")[:20],
                    entry.get("asset_class", "")[:5],
                    str(entry.get("shares", 0)),
                    eur(entry.get("entry_price")),
                    eur(entry.get("position_value_eur")),
                    eur(entry.get("initial_stop") or entry.get("stop_loss")),
                    num(entry.get("adx"), 1),
                    pct(entry.get("atr_pct")),
                    f"{entry.get('momentum_score', 0):.1f}"
                ])
            
            t = Table(entry_data, colWidths=[8*mm, 20*mm, 34*mm, 10*mm, 10*mm, 17*mm, 17*mm, 17*mm, 12*mm, 13*mm, 16*mm])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), C_SUCCESS),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 7),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, C_LIGHT_BG]),
            ]))
            story.append(t)
        else:
            story.append(Paragraph("No new entries this cycle.", S["body"]))
        
        story.append(Spacer(1, 6 * mm))
        
        # Capital Summary
        cs = scenario_data.get("capital_summary", {})
        
        story.append(Paragraph("CAPITAL AND ALLOCATION SUMMARY", S["section_h1"]))
        story.append(Spacer(1, 3 * mm))
        
        cap_data = [
            ["Account Equity", eur(cs.get("account_equity_eur", 0))],
            ["Holds Value", eur(cs.get("holds_value_eur", 0))],
            ["Capital Freed by Exits", f"{eur(cs.get('exits_freed_eur_approx', 0))} (approx)"],
            ["Capital Required - Entries", eur(cs.get("entries_required_eur", 0))],
            ["Total Deployed (after)", f"{eur(cs.get('total_deployed_after_eur', 0))} ({cs.get('total_deployed_after_pct', 0):.1f}%)"],
            ["Cash Remaining (approx)", f"{eur(cs.get('cash_remaining_approx_eur', 0))} ({cs.get('cash_remaining_approx_pct', 0):.1f}%)"],
        ]
        
        t = Table(cap_data, colWidths=[90*mm, 70*mm])
        t.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('ALIGN', (0, 0), (0, -1), 'LEFT'),
            ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('ROWBACKGROUNDS', (0, 0), (-1, -1), [C_LIGHT_BG, colors.white]),
        ]))
        story.append(t)
        story.append(Spacer(1, 6 * mm))
        
        # Execution Checklist
        story.append(Paragraph("EXECUTION CHECKLIST", S["section_h1"]))
        story.append(Spacer(1, 3 * mm))
        
        checklist = [
            "1. <b>Verify scenario choice:</b> Confirm this is your chosen scenario from the comparison",
            f"2. <b>Review all recommendations:</b> Check exits ({len(mandatory) + len(rotation)}) and entries ({len(entries)})",
            "3. <b>Verify data freshness:</b> Run Script 01 --mode incremental if needed",
            f"4. <b>On {scenario_data.get('execution_date', '—')}:</b> Execute exit orders at market open",
            f"5. <b>On {scenario_data.get('execution_date', '—')}:</b> Place limit buy orders at close + 0.5%",
            "6. <b>After fills:</b> Confirm stop-loss orders are live for all new positions",
            "7. <b>Log executions:</b> Use Script 13 (13_log_execution.py)",
            "8. <b>Weekly monitoring:</b> Verify stop-loss orders every Friday using Script 09"
        ]
        
        for item in checklist:
            story.append(Paragraph(f"☐ {item}", S["body"]))
        
        story.append(Spacer(1, 6 * mm))
        
        # Warnings
        warnings = scenario_data.get("warnings", [])
        if warnings:
            story.append(Paragraph("⚠ WARNINGS", S["alert_warning"]))
            story.append(Spacer(1, 2 * mm))
            for warn in warnings:
                story.append(Paragraph(f"• {warn}", S["body"]))
            story.append(Spacer(1, 5 * mm))
        
        # Page break after each scenario
        if scenario_num < 3:
            story.append(PageBreak())
    
    # ========================================================================
    # PART 5: FINAL DECISION PAGE
    # ========================================================================
    
    story.append(PageBreak())
    story.append(Spacer(1, 20 * mm))
    story.append(Paragraph("FINAL DECISION REQUIRED", S["cover_title"]))
    story.append(Spacer(1, 8 * mm))
    
    story.append(Paragraph(
        "<b>You have reviewed three complete scenarios:</b>",
        S["section_h2"]
    ))
    story.append(Spacer(1, 3 * mm))
    
    decision_text = [
        "• <b>Scenario 1 (Pure Momentum):</b> Highest momentum, may sacrifice diversification",
        "• <b>Scenario 2 (Force Diversity):</b> Enforced asset class balance, lower momentum",
        "• <b>Scenario 3 (Balanced):</b> Middle ground between momentum and diversification"
    ]
    
    for text in decision_text:
        story.append(Paragraph(text, S["body"]))
    
    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph(
        "<b>Next Steps:</b>",
        S["section_h2"]
    ))
    story.append(Spacer(1, 3 * mm))
    
    next_steps = [
        f"1. <b>Choose ONE scenario</b> based on your analysis of the comparison and execution details",
        f"2. <b>Execute on {rec.get('execution_date', '—')}</b> following the checklist for your chosen scenario",
        "3. <b>Log all trades</b> using Script 13 for audit trail",
        "4. <b>Monitor positions</b> weekly and at next monthly rebalance"
    ]
    
    for step in next_steps:
        story.append(Paragraph(step, S["body"]))
    
    story.append(Spacer(1, 15 * mm))
    story.append(HRFlowable(width="100%", thickness=2, color=C_NAVY))
    story.append(Spacer(1, 5 * mm))
    story.append(Paragraph(
        f'<para alignment="center" textColor="{C_DANGER}"><b>⚠ HUMAN APPROVAL REQUIRED BEFORE PLACING ANY ORDERS</b></para>',
        S["body"]
    ))
    story.append(Paragraph(
        f'<para alignment="center">Execution date: {rec.get("execution_date", "—")} | '
        f'Generated: {rec.get("generated_at", "—")[:19]} | '
        f'Architecture: {rec.get("architecture_version", "v3.2")}</para>',
        S["body_small"]
    ))
    
    # ========================================================================
    # PART 6: TOP-50 CANDIDATE UNIVERSE TABLE (appendix — pick your own BUYs)
    # ========================================================================
    story.extend(build_top50_table(rec, S))

    # Build PDF
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    logger.info(f"Multi-scenario PDF complete: {len(story)} elements")


# ============================================================================
# PDF BUILDER
# ============================================================================

def build_halted_pdf(rec: Dict, output_path: Path, strategy_name: str = '') -> None:
    """
    Produce a minimal PDF for a HALT_ALL circuit-breaker state.

    Script 11 saves a JSON with no scenario or trade data when all trading is
    suspended.  Rather than crashing, Script 12 renders a clear one-page report
    that shows:
        - Red HALT banner
        - All triggered circuit breakers with severity, value and action required
        - Metadata (date, equity, generated_at)
        - Execution instruction: fix the root cause and rerun Scripts 01-11
    """
    month = rec.get("month") or rec.get("rebalance_date", "unknown")[:7]
    S     = build_styles()

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=MARGIN_H,
        rightMargin=MARGIN_H,
        topMargin=MARGIN_V + 8 * mm,
        bottomMargin=MARGIN_V + 7 * mm,
        title=f"Rebalancing Recommendations {month} — HALTED",
        author="Multi-Asset Trend Following Strategy v3.2",
    )
    on_page = _make_page_decorator(month)
    story: List = []

    # ── Red HALT banner ──────────────────────────────────────────────────────
    story.append(Spacer(1, 8))
    halt_banner = Table(
        [[Paragraph("🛑  ALL TRADING SUSPENDED — CRITICAL CIRCUIT BREAKER", S["alert_danger"])]],
        colWidths=[CONTENT_W],
    )
    halt_banner.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), C_DANGER),
        ("TOPPADDING",    (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ("LEFTPADDING",   (0, 0), (-1, -1), 14),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 14),
    ]))
    story.append(halt_banner)
    story.append(Spacer(1, 12))

    # ── Metadata ─────────────────────────────────────────────────────────────
    story.append(section_header("REPORT METADATA", S, color=C_NAVY))
    story.append(Spacer(1, 4))
    meta_rows = [
        ("Period",            safe(rec.get("month") or month)),
        ("Rebalance Date",    safe(rec.get("rebalance_date"))),
        ("Execution Date",    safe(rec.get("execution_date"))),
        ("Account Equity",    eur(rec.get("account_equity"))),
        ("Generated At",      safe(rec.get("generated_at"))),
        ("Architecture",      safe(rec.get("architecture_version", "v3.2"))),
        ("Status",            safe(rec.get("status"))),
    ]
    story.append(mini_kv_table(meta_rows, S,
                                col_widths=[CONTENT_W * 0.35, CONTENT_W * 0.65]))
    story.append(Spacer(1, 14))

    # ── Circuit breakers detail ───────────────────────────────────────────────
    story += build_circuit_breakers(rec, S)
    story.append(Spacer(1, 14))

    # ── Required action ──────────────────────────────────────────────────────
    story.append(section_header("ACTION REQUIRED BEFORE NEXT RUN", S, color=C_ORANGE))
    story.append(Spacer(1, 6))
    actions = [
        "1. Identify and resolve every circuit breaker listed above.",
        "2. Run Script 01 (--mode incremental) to refresh market data.",
        "3. Rerun Scripts 03 → 10 in order to rebuild indicators and signals.",
        "4. Rerun Script 11 to generate a fresh recommendation JSON.",
        "5. Rerun Script 12 to produce the updated PDF report.",
        "6. Use --override-circuit-breaker ONLY if the trigger is a confirmed data error "
           "and you have manually verified the underlying condition is safe.",
    ]
    for action in actions:
        story.append(Paragraph(action, S["checklist_item"]))
    story.append(Spacer(1, 14))

    # ── Footer ────────────────────────────────────────────────────────────────
    footer = Table(
        [[Paragraph("NO ORDERS MAY BE PLACED WHILE TRADING IS HALTED", S["footer"])]],
        colWidths=[CONTENT_W],
    )
    footer.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), C_DANGER),
        ("TOPPADDING",    (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING",   (0, 0), (-1, -1), 14),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 14),
    ]))
    story.append(footer)
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        f"Execution date: {safe(rec.get('execution_date'))}  |  "
        f"Generated: {safe(rec.get('generated_at'))}  |  "
        f"Architecture: {safe(rec.get('architecture_version', 'v3.2'))}",
        S["body_italic"],
    ))

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    logger.info(f"Halted-state PDF written ({output_path.stat().st_size / 1024:.1f} KB)")
    if strategy_name:
        logger.info(f'Strategy: {strategy_name}')


def build_pdf(rec: Dict, output_path: Path, strategy_name: str = '') -> None:
    """
    Assemble all sections and produce the PDF.
    
    Auto-detects format:
        - Multi-scenario: Builds comprehensive PDF with comparison + 3 execution plans
        - Single-scenario: Builds traditional execution PDF (backward compatible)
    """
    # Detect format
    format_type = detect_format(rec)
    logger.info(f"Detected format: {format_type}")

    if format_type == 'halted':
        # All trading suspended — produce a minimal circuit-breaker report
        logger.warning("Status is HALTED — generating circuit-breaker report (no trade recommendations).")
        build_halted_pdf(rec, output_path, strategy_name=strategy_name)
        return

    if format_type == 'multi':
        # Multi-scenario format: Build comprehensive comparison + execution PDF
        build_multi_scenario_pdf(rec, output_path, strategy_name=strategy_name)
        return
    
    # Single-scenario format: Use original logic below
    logger.info("Building single-scenario PDF (legacy format)...")
    
    month = rec.get("month", "unknown")
    S     = build_styles()

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=MARGIN_H,
        rightMargin=MARGIN_H,
        topMargin=MARGIN_V + 8 * mm,
        bottomMargin=MARGIN_V + 7 * mm,
        title=f"Rebalancing Recommendations {month}",
        author="Multi-Asset Trend Following Strategy v3.2",
        subject=f"Monthly recommendations for {month}",
    )

    on_page = _make_page_decorator(month)

    story: List = []

    story += build_cover(rec, S)
    story.append(PageBreak())
    story += build_circuit_breakers(rec, S)
    story += build_exits(rec, S)
    story.append(PageBreak())
    story += build_entries(rec, S)
    story.append(PageBreak())
    story += build_holds(rec, S)
    story.append(PageBreak())
    story += build_capital(rec, S)
    story += build_checklist(rec, S)

    logger.info(f"Building PDF -> {output_path}")
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    logger.info(f"PDF written ({output_path.stat().st_size / 1024:.1f} KB)")


# ============================================================================
# INPUT RESOLUTION
# ============================================================================

def resolve_input_file(month: Optional[str], input_path: Optional[str]) -> Path:
    """
    Resolve the JSON file to process.

    Priority:
      1. --input (explicit path)
      2. --month (YYYY-MM)
      3. Auto-detect latest file in reports/rebalancing/
    """
    if input_path:
        p = Path(input_path)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        if not p.exists():
            raise FileNotFoundError(f"--input file not found: {p}")
        return p

    if month:
        p = REPORTS_DIR / f"{month}_recommendations.json"
        if not p.exists():
            raise FileNotFoundError(
                f"No recommendations file for month {month}: {p}\n"
                "Run Script 11 first to generate the JSON."
            )
        return p

    # Auto-detect latest
    candidates = sorted(REPORTS_DIR.glob("*_recommendations.json"))
    if not candidates:
        raise FileNotFoundError(
            f"No recommendations JSON files found in {REPORTS_DIR}.\n"
            "Run Script 11 (11_monthly_rebalancing.py) first."
        )
    latest = candidates[-1]
    logger.info(f"Auto-detected latest: {latest.name}")
    return latest


def load_recommendations(json_path: Path) -> Dict:
    """Load and validate a recommendations JSON file."""
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    required = {"status", "rebalance_date", "execution_date"}
    missing  = required - set(data.keys())
    if missing:
        raise ValueError(
            f"Recommendations JSON is missing required keys: {missing}. "
            "Ensure Script 11 produced this file correctly."
        )
    return data


# ============================================================================
# HTML DASHBOARD  (Script 12 combined output — one file for all strategies)
# ============================================================================

# Sentinel the template must contain where the JS strategies array is injected.
_JS_SENTINEL = "/*INJECT_STRATEGIES*/"
_TS_SENTINEL = "<!-- GENERATED_AT -->"


def _formula_desc(formula: str, rec: Dict) -> str:
    """Return human-readable formula description for the dashboard formula tag."""
    if formula in ("sma_distance", "sma_dist"):
        return "Score = (Close \u2212 SMA\u2082\u2080\u2080) / SMA\u2082\u2080\u2080 \u00d7 100"
    if formula in ("roc_weighted", "roc_weight"):
        mom = rec.get("momentum", {})
        w   = mom.get("roc_weights", [0.20, 0.30, 0.50])
        p   = mom.get("roc_periods", [20, 60, 120])
        terms = " + ".join(f"{wi:.2f}\u00d7ROC{pi}" for wi, pi in zip(w, p))
        return f"Score = {terms}"
    return formula


def _load_formula_from_strategy(strategy) -> str:
    """
    Read momentum.formula from the strategy's config JSON.
    Falls back to 'sma_distance' if the file is absent or malformed.
    """
    try:
        cfg_path = getattr(strategy, "config_path", None)
        if cfg_path and Path(cfg_path).exists():
            with open(cfg_path, encoding="utf-8") as fh:
                cfg = json.load(fh)
            return cfg.get("momentum", {}).get("formula", "sma_distance")
    except Exception:
        pass
    return "sma_distance"


def _slim_rec(rec: Dict) -> Dict:
    """
    Return a copy of *rec* stripped of large / verbose fields that the
    dashboard does not use, keeping the JSON payload small.
    """
    drop = {"execution_checklist"}
    slim = {k: v for k, v in rec.items() if k not in drop}
    # Cap ranked_candidates at 50 rows
    if "ranked_candidates" in slim:
        slim["ranked_candidates"] = slim["ranked_candidates"][:50]
    return slim


def _dashboard_template_html() -> str:
    """
    Return the full dashboard HTML template as a Python string.

    The template contains two sentinels:
        /*INJECT_STRATEGIES*/   — replaced with the JS STRATEGIES array
        <!-- GENERATED_AT -->   — replaced with a generation timestamp comment

    This embeds the complete self-contained dashboard so Script 12 has
    zero external file dependencies.
    """
    # The template is defined inline so the script is fully self-contained.
    # It is identical in structure to the standalone dashboard HTML file
    # (trend_recommendation_dashboard.html) but with the static STRATEGIES
    # constant replaced by the /*INJECT_STRATEGIES*/ sentinel.
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<!-- GENERATED_AT -->
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>TrendFollowing OS \u2014 Recommendation Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap" rel="stylesheet"/>
<style>
:root{--bg:#0d1117;--bg2:#161b22;--bg3:#21262d;--bg4:#30363d;--bd:#30363d;--bd2:#444c56;--txt:#c9d1d9;--txt2:#8b949e;--txt3:#484f58;--blue:#58a6ff;--green:#3fb950;--yellow:#d29922;--red:#f85149;--purple:#a371f7;--s1c:#58a6ff;--s1bg:#051d4d;--s1bd:#1f6feb;--s2c:#3fb950;--s2bg:#0d2a1e;--s2bd:#238636;--s3c:#a371f7;--s3bg:#1f1235;--s3bd:#6e40c9;--cm:#f85149;--cmbg:#3d1a1a;--cmbd:#8d1f1f;--cr:#d29922;--crbg:#2d2210;--crbd:#7a5d15;--cb2:#3fb950;--cb2bg:#0d2a1e;--cb2bd:#238636;--ch:#58a6ff;--chbg:#051d4d;--chbd:#1f6feb;--st1c:#e6b450;--st1bg:#1f1a09;--st1bd:#7a5d15;--st2c:#79c0ff;--st2bg:#031526;--st2bd:#0d5a9e;--st3c:#f778ba;--st3bg:#1f0a1a;--st3bd:#8d1f6e;--st4c:#56d364;--st4bg:#091a0e;--st4bd:#196c2e;--st5c:#d2a8ff;--st5bg:#1a0d2d;--st5bd:#6e40c9;--mono:'IBM Plex Mono',monospace;--sans:'IBM Plex Sans',sans-serif;--r:6px}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--sans);background:var(--bg);color:var(--txt);font-size:13px;line-height:1.5;min-height:100vh}
::-webkit-scrollbar{width:5px;height:5px}::-webkit-scrollbar-track{background:var(--bg)}::-webkit-scrollbar-thumb{background:var(--bg4);border-radius:3px}
.hdr{background:var(--bg2);border-bottom:1px solid var(--bd);padding:10px 18px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:300}
.hdr-l{display:flex;align-items:center;gap:10px}
.logo{width:26px;height:26px;background:linear-gradient(135deg,#1f6feb,#6e40c9);border-radius:5px;display:flex;align-items:center;justify-content:center;font-family:var(--mono);font-size:10px;font-weight:600;color:#fff;flex-shrink:0}
.hdr-t{font-family:var(--mono);font-size:12px;font-weight:500}.hdr-s{font-size:10px;color:var(--txt2);margin-top:1px}
.hdr-r{display:flex;align-items:center;gap:14px}
.live-dot{width:6px;height:6px;border-radius:50%;background:var(--yellow);animation:blink 2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.live-lbl{font-family:var(--mono);font-size:10px;color:var(--yellow);display:flex;align-items:center;gap:5px;letter-spacing:.05em}
.hdr-m{font-family:var(--mono);font-size:10px;color:var(--txt2)}
.strat-bar{background:var(--bg2);border-bottom:1px solid var(--bd);padding:0 18px;display:flex;align-items:stretch;gap:0;position:sticky;top:46px;z-index:200;overflow-x:auto}
.stab{display:flex;align-items:center;gap:8px;padding:9px 16px 9px 12px;border-bottom:2px solid transparent;cursor:pointer;transition:all .15s;font-size:12px;color:var(--txt2);border-right:1px solid var(--bd);user-select:none;flex-shrink:0}
.stab:hover{background:var(--bg3);color:var(--txt)}
.stab.act-st1{border-bottom-color:var(--st1c);color:var(--st1c);background:var(--st1bg)}
.stab.act-st2{border-bottom-color:var(--st2c);color:var(--st2c);background:var(--st2bg)}
.stab.act-st3{border-bottom-color:var(--st3c);color:var(--st3c);background:var(--st3bg)}
.stab.act-st4{border-bottom-color:var(--st4c);color:var(--st4c);background:var(--st4bg)}
.stab.act-st5{border-bottom-color:var(--st5c);color:var(--st5c);background:var(--st5bg)}
.stab-name{font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:.04em}
.stab-label{font-size:10px;opacity:.8}
.sbadge{font-family:var(--mono);font-size:8px;padding:1px 6px;border-radius:3px;margin-left:4px;border:1px solid}
.sb-live{background:#071d0e;color:var(--green);border-color:#196c2e}
.sb-paper{background:var(--crbg);color:var(--yellow);border-color:var(--crbd)}
.stab-formula{font-family:var(--mono);font-size:9px;color:var(--txt3);margin-left:2px}
.cmp-btn{font-family:var(--mono);font-size:10px;padding:5px 12px;border-radius:4px;border:1px solid var(--bd2);background:var(--bg3);color:var(--txt2);cursor:pointer;transition:all .15s;margin-left:auto;align-self:center;flex-shrink:0;letter-spacing:.03em}
.cmp-btn:hover{border-color:var(--blue);color:var(--blue)}
.cmp-btn.active{background:var(--s1bg);color:var(--blue);border-color:var(--blue)}
.kstrip{background:var(--bg2);border-bottom:1px solid var(--bd);display:flex;overflow-x:auto;padding:0 18px}
.k{padding:9px 16px 9px 0;border-right:1px solid var(--bd);margin-right:16px;flex-shrink:0}.k:last-child{border-right:none;margin-right:0}
.kl{font-size:9px;color:var(--txt2);text-transform:uppercase;letter-spacing:.07em}
.kv{font-family:var(--mono);font-size:18px;font-weight:600;margin-top:1px}
.ks{font-size:9px;color:var(--txt2);margin-top:1px}
.c-blue{color:var(--blue)}.c-green{color:var(--green)}.c-yellow{color:var(--yellow)}.c-red{color:var(--red)}.c-s1{color:var(--s1c)}.c-s2{color:var(--s2c)}.c-s3{color:var(--s3c)}
.main{padding:14px 18px}
.sec{font-family:var(--mono);font-size:9px;font-weight:500;color:var(--txt2);letter-spacing:.1em;text-transform:uppercase;margin-bottom:7px;margin-top:2px}
.cmp-banner{background:#071426;border:1px solid #0d5a9e;border-radius:var(--r);padding:10px 14px;margin-bottom:12px;display:flex;align-items:flex-start;gap:10px}
.cmp-icon{font-family:var(--mono);font-size:14px;color:var(--blue);flex-shrink:0}
.cmp-txt{font-size:11px;color:var(--txt2);line-height:1.6}.cmp-txt strong{color:var(--txt)}
.hidden{display:none!important}
.cmp-grid{display:grid;gap:10px;margin-bottom:12px}
.cmp-col{border:1px solid var(--bd);border-radius:var(--r);overflow:hidden}
.cmp-col-hdr{padding:8px 12px;font-family:var(--mono);font-size:10px;font-weight:600;letter-spacing:.05em;display:flex;align-items:center;gap:8px}
.ch-st1{background:var(--st1bg);color:var(--st1c);border-bottom:1px solid var(--st1bd)}
.ch-st2{background:var(--st2bg);color:var(--st2c);border-bottom:1px solid var(--st2bd)}
.ch-st3{background:var(--st3bg);color:var(--st3c);border-bottom:1px solid var(--st3bd)}
.ch-st4{background:var(--st4bg);color:var(--st4c);border-bottom:1px solid var(--st4bd)}
.ch-st5{background:var(--st5bg);color:var(--st5c);border-bottom:1px solid var(--st5bd)}
.cmp-sym-list{padding:8px 12px;display:flex;flex-direction:column;gap:3px;max-height:320px;overflow-y:auto}
.cmp-row{display:flex;align-items:center;justify-content:space-between;padding:3px 6px;border-radius:4px;font-size:11px}
.cmp-row.both{background:#0d1a2d;border:1px solid #1f3a6e}
.cmp-row.unique{background:var(--bg3)}
.cmp-sym{font-family:var(--mono);font-weight:500}
.cmp-both{font-family:var(--mono);font-size:8px;padding:1px 5px;border-radius:3px;background:#0d1a2d;color:var(--blue);border:1px solid #1f6feb}
.cmp-act{font-family:var(--mono);font-size:9px}
.act-buy{color:var(--green)}.act-hold{color:var(--blue)}.act-exit{color:var(--red)}.act-rot{color:var(--yellow)}
.cbp{background:var(--bg2);border:1px solid var(--bd);border-radius:var(--r);padding:11px 13px;margin-bottom:12px;display:flex;gap:10px;flex-wrap:wrap;align-items:flex-start}
.cb-box{flex-shrink:0;display:flex;align-items:center;gap:7px}
.cb-dot{width:9px;height:9px;border-radius:50%}.cb-clear{background:var(--green)}.cb-warn{background:var(--yellow)}.cb-halt{background:var(--red)}
.cb-t{font-family:var(--mono);font-size:11px;font-weight:500}
.cb-sep{width:1px;background:var(--bd);align-self:stretch;flex-shrink:0}
.cb-items{display:flex;gap:6px;flex-wrap:wrap;flex:1}
.cbi{padding:2px 8px;border-radius:4px;font-size:10px;border:1px solid;font-family:var(--mono);display:flex;align-items:center;gap:4px}
.cbi-ok{background:#071d0e;border-color:#196c2e;color:var(--green)}.cbi-warn{background:var(--crbg);border-color:var(--crbd);color:var(--yellow)}.cbi-crit{background:var(--cmbg);border-color:var(--cmbd);color:var(--red)}
.cbi-d{width:4px;height:4px;border-radius:50%;flex-shrink:0}
.srow{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px}
.scard{border-radius:var(--r);border:1px solid;padding:10px 12px;cursor:pointer;transition:opacity .15s}
.scard:hover{opacity:.82}
.sc-s1{background:var(--s1bg);border-color:var(--s1bd)}.sc-s2{background:var(--s2bg);border-color:var(--s2bd)}.sc-s3{background:var(--s3bg);border-color:var(--s3bd)}
.sc-name{font-family:var(--mono);font-size:11px;font-weight:600;margin-bottom:3px}
.sc-s1 .sc-name{color:var(--s1c)}.sc-s2 .sc-name{color:var(--s2c)}.sc-s3 .sc-name{color:var(--s3c)}
.sc-phil{font-size:10px;color:var(--txt2);line-height:1.5;margin-bottom:6px}
.sc-stats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:6px}
.sc-stat{font-size:10px;color:var(--txt2)}.sc-stat span{font-family:var(--mono);font-weight:500}
.sc-s1 .sc-stat span{color:var(--s1c)}.sc-s2 .sc-stat span{color:var(--s2c)}.sc-s3 .sc-stat span{color:var(--s3c)}
.sc-pills{display:flex;gap:4px;flex-wrap:wrap}
.spill{font-family:var(--mono);font-size:9px;padding:2px 6px;border-radius:3px;border:1px solid}
.sp-mand{background:var(--cmbg);color:var(--cm);border-color:var(--cmbd)}.sp-rot{background:var(--crbg);color:var(--cr);border-color:var(--crbd)}.sp-buy{background:var(--cb2bg);color:var(--cb2);border-color:var(--cb2bd)}.sp-hold{background:var(--chbg);color:var(--ch);border-color:var(--chbd)}.sp-warn{background:#2d1c08;color:var(--yellow);border-color:#7a5d15}
.ftag{font-family:var(--mono);font-size:9px;padding:3px 10px;border-radius:3px;border:1px solid;margin-bottom:10px;display:inline-flex;align-items:center;gap:6px}
.ft-dot{width:5px;height:5px;border-radius:50%}
.ft-st1{background:var(--st1bg);color:var(--st1c);border-color:var(--st1bd)}.ft-st2{background:var(--st2bg);color:var(--st2c);border-color:var(--st2bd)}.ft-st3{background:var(--st3bg);color:var(--st3c);border-color:var(--st3bd)}.ft-st4{background:var(--st4bg);color:var(--st4c);border-color:var(--st4bd)}.ft-st5{background:var(--st5bg);color:var(--st5c);border-color:var(--st5bd)}
.fbar{display:flex;gap:7px;margin-bottom:9px;flex-wrap:wrap;align-items:center}
.fl{font-size:9px;color:var(--txt2);text-transform:uppercase;letter-spacing:.07em;white-space:nowrap}
select,input[type=text]{background:var(--bg2);border:1px solid var(--bd);border-radius:5px;color:var(--txt);font-family:var(--mono);font-size:11px;padding:4px 7px;outline:none;cursor:pointer;transition:border-color .15s}
select:hover,input[type=text]:hover{border-color:var(--bd2)}select:focus,input[type=text]:focus{border-color:var(--blue)}
input[type=text]{width:140px}
.fsep{width:1px;height:20px;background:var(--bd);margin:0 2px}
.fbtns{display:flex;gap:3px}
.fb{font-family:var(--mono);font-size:9px;font-weight:500;padding:3px 8px;border-radius:4px;border:1px solid var(--bd2);background:var(--bg2);color:var(--txt2);cursor:pointer;transition:all .15s;letter-spacing:.04em}
.fb:hover{background:var(--bg3);color:var(--txt)}
.fb.fa-all{background:var(--bg4);color:var(--txt);border-color:var(--bd2)}.fb.fa-mand{background:var(--cmbg);color:var(--cm);border-color:var(--cmbd)}.fb.fa-rot{background:var(--crbg);color:var(--cr);border-color:var(--crbd)}.fb.fa-buy{background:var(--cb2bg);color:var(--cb2);border-color:var(--cb2bd)}.fb.fa-hold{background:var(--chbg);color:var(--ch);border-color:var(--chbd)}
.fb.fr-s1{background:var(--s1bg);color:var(--s1c);border-color:var(--s1bd)}.fb.fr-s2{background:var(--s2bg);color:var(--s2c);border-color:var(--s2bd)}.fb.fr-s3{background:var(--s3bg);color:var(--s3c);border-color:var(--s3bd)}.fb.fr-any{background:#1c1c24;color:#c7a9f8;border-color:#6e40c9}
.rct{font-size:10px;color:var(--txt2);font-family:var(--mono);margin-left:auto}.rct span{color:var(--txt);font-weight:500}
.leg{display:flex;gap:12px;margin-bottom:9px;padding:7px 12px;background:var(--bg2);border:1px solid var(--bd);border-radius:var(--r);font-size:10px;align-items:center;flex-wrap:wrap}
.leg-d{width:7px;height:7px;border-radius:2px;flex-shrink:0}.leg-i{display:flex;align-items:center;gap:5px;color:var(--txt2)}.leg-t{color:var(--txt);font-weight:500}
.tw{overflow-x:auto;border-radius:var(--r);border:1px solid var(--bd)}
table{width:100%;border-collapse:collapse;font-size:11px}
thead tr{background:var(--bg2);border-bottom:1px solid var(--bd)}
th{font-family:var(--mono);font-size:9px;font-weight:500;color:var(--txt2);letter-spacing:.07em;text-transform:uppercase;padding:7px 9px;text-align:right;white-space:nowrap;border-right:1px solid var(--bd);cursor:pointer;user-select:none}
th.tl{text-align:left}th:last-child{border-right:none}th:hover{color:var(--txt)}th .sa{margin-left:2px;opacity:.3}
tbody tr{border-bottom:1px solid var(--bd);transition:background .1s}tbody tr:last-child{border-bottom:none}tbody tr:hover{filter:brightness(1.12)}
.row-mand{background:#160a0a}.row-rot{background:#14120a}.row-buy{background:#091410}.row-hold{background:#070e18}
td{padding:8px 9px;text-align:right;border-right:1px solid var(--bd);font-family:var(--mono);font-size:11px;white-space:nowrap;vertical-align:middle}td:last-child{border-right:none}td.tl{text-align:left;font-family:var(--sans)}
.chip{display:inline-block;font-family:var(--mono);font-size:9px;font-weight:600;padding:2px 7px;border-radius:3px;border:1px solid;letter-spacing:.05em}
.ch-mand{background:var(--cmbg);color:var(--cm);border-color:var(--cmbd)}.ch-rot{background:var(--crbg);color:var(--cr);border-color:var(--crbd)}.ch-buy{background:var(--cb2bg);color:var(--cb2);border-color:var(--cb2bd)}.ch-hold{background:var(--chbg);color:var(--ch);border-color:var(--chbd)}
.sba{display:flex;gap:3px}
.sb{font-family:var(--mono);font-size:8px;font-weight:500;padding:1px 5px;border-radius:3px;border:1px solid}
.sb-s1{background:var(--s1bg);color:var(--s1c);border-color:var(--s1bd)}.sb-s2{background:var(--s2bg);color:var(--s2c);border-color:var(--s2bd)}.sb-s3{background:var(--s3bg);color:var(--s3c);border-color:var(--s3bd)}
.rt{font-family:var(--mono);font-size:9px;padding:1px 6px;border-radius:3px;border:1px solid}
.rt-stop{color:var(--red);background:var(--cmbg);border-color:var(--cmbd)}.rt-rev{color:#ff8080;background:#3d1010;border-color:#8d2020}.rt-weak{color:var(--yellow);background:var(--crbg);border-color:var(--crbd)}.rt-drop{color:#c9a020;background:var(--crbg);border-color:var(--crbd)}
.sym{font-family:var(--mono);font-weight:500;color:var(--txt);font-size:12px}.snm{font-size:10px;color:var(--txt2);max-width:130px;overflow:hidden;text-overflow:ellipsis;margin-top:1px}
.ap{font-family:var(--mono);font-size:8px;padding:1px 4px;border-radius:3px;background:var(--bg3);color:var(--txt2);border:1px solid var(--bd);margin-left:3px}
.sbw{display:flex;align-items:center;gap:5px;justify-content:flex-end}
.sb-bg{width:48px;height:3px;background:var(--bg3);border-radius:2px;overflow:hidden}
.sb-f{height:100%;border-radius:2px}
.sf-st1{background:linear-gradient(90deg,#7a5d15,#e6b450)}.sf-st2{background:linear-gradient(90deg,#0d5a9e,#79c0ff)}.sf-st3{background:linear-gradient(90deg,#8d1f6e,#f778ba)}.sf-st4{background:linear-gradient(90deg,#196c2e,#56d364)}.sf-st5{background:linear-gradient(90deg,#6e40c9,#d2a8ff)}
.adxw{display:flex;align-items:center;gap:4px;justify-content:flex-end}
.adxp{width:5px;height:5px;border-radius:50%}.adxs{background:var(--green)}.adxm{background:var(--yellow)}.adxw2{background:var(--red)}
.stw{display:flex;flex-direction:column;align-items:flex-end;gap:1px}
.stb{font-size:8px;padding:1px 4px;border-radius:2px;font-family:var(--mono)}
.st-init{background:var(--bg3);color:var(--txt2)}.st-trail{background:#1f1235;color:var(--purple);border:1px solid #6e40c9}
.prio{font-family:var(--mono);font-size:8px;padding:1px 4px;border-radius:3px}
.p1{background:#3d1a1a;color:#ff6b6b;border:1px solid #8d1f1f}.p2{background:#2d1a0a;color:#e8a23b;border:1px solid #7a5d15}.p3{background:var(--crbg);color:var(--yellow);border:1px solid var(--crbd)}.p4{background:var(--bg3);color:var(--txt2);border:1px solid var(--bd)}
.ot{font-family:var(--mono);font-size:8px;color:var(--txt3)}
.pos{color:var(--green)}.neg{color:var(--red)}
.empty{text-align:center;padding:40px;color:var(--txt2);font-size:12px}
.empty-i{display:block;font-family:var(--mono);font-size:22px;color:var(--txt3);margin-bottom:6px}
.ftr{margin-top:16px;padding:10px 18px;border-top:1px solid var(--bd);display:flex;justify-content:space-between;align-items:center;font-size:9px;color:var(--txt3);font-family:var(--mono);flex-wrap:wrap;gap:4px}
</style>
</head>
<body>
<div class="hdr">
  <div class="hdr-l">
    <div class="logo">TF</div>
    <div>
      <div class="hdr-t">TrendFollowing OS \u00b7 Monthly Rebalancing Dashboard</div>
      <div class="hdr-s">Script 12 output \u00b7 Multi-strategy view \u00b7 Human approval required before execution</div>
    </div>
  </div>
  <div class="hdr-r">
    <div class="live-lbl"><div class="live-dot"></div><span id="deploy-lbl">PAPER TRADING</span></div>
    <div class="hdr-m" id="hdr-m">\u2014</div>
  </div>
</div>
<div class="strat-bar" id="strat-bar">
  <button class="cmp-btn" id="cmp-btn" onclick="toggleCmp()">&#8644; Compare Strategies</button>
</div>
<div class="kstrip">
  <div class="k"><div class="kl">Account Equity</div><div class="kv c-blue" id="kv-eq">\u2014</div><div class="ks">EUR</div></div>
  <div class="k"><div class="kl">Trend Qualified</div><div class="kv c-green" id="kv-q">\u2014</div><div class="ks">pass SMA+ADX gate</div></div>
  <div class="k"><div class="kl">Max Positions</div><div class="kv" id="kv-mp">\u2014</div><div class="ks">at this equity</div></div>
  <div class="k"><div class="kl">Mandatory Exits</div><div class="kv c-red" id="kv-me">\u2014</div><div class="ks">P1\u20133: stop/rev/weak</div></div>
  <div class="k"><div class="kl">S1 Entries</div><div class="kv c-s1" id="kv-e1">\u2014</div><div class="ks">pure momentum</div></div>
  <div class="k"><div class="kl">S2 Entries</div><div class="kv c-s2" id="kv-e2">\u2014</div><div class="ks">force diversity</div></div>
  <div class="k"><div class="kl">S3 Entries</div><div class="kv c-s3" id="kv-e3">\u2014</div><div class="ks">balanced</div></div>
  <div class="k"><div class="kl">Circuit Breakers</div><div class="kv" id="kv-cb" style="font-size:13px">\u2014</div><div class="ks" id="ks-cb">\u2014</div></div>
  <div class="k"><div class="kl">Execution Date</div><div class="kv" id="kv-ed" style="font-size:12px">\u2014</div><div class="ks">first trading day</div></div>
</div>
<div class="main">
  <div id="cmp-view" class="hidden">
    <div class="cmp-banner">
      <div class="cmp-icon">&#8644;</div>
      <div class="cmp-txt"><strong>Strategy comparison</strong> \u2014 symbols in <span style="color:var(--blue)">all strategies</span> are highlighted. Showing active scenario filter (S1/S2/S3/ANY).</div>
    </div>
    <div class="cmp-grid" id="cmp-grid"></div>
  </div>
  <div id="single-view">
    <div id="ftag-wrap"></div>
    <div class="sec">Circuit Breaker Status</div>
    <div class="cbp" id="cbp">
      <div class="cb-box"><div class="cb-dot" id="cb-dot"></div><div class="cb-t" id="cb-t">\u2014</div></div>
      <div class="cb-sep"></div>
      <div class="cb-items" id="cb-items"></div>
    </div>
    <div class="sec">Three-scenario comparison \u2014 click to filter table</div>
    <div class="srow" id="srow"></div>
    <div class="fbar">
      <div class="fl">Search</div>
      <input type="text" id="srch" placeholder="symbol or name\u2026" oninput="R()"/>
      <div class="fsep"></div>
      <div class="fl">Action</div>
      <div class="fbtns" id="abtns">
        <button class="fb fa-all" data-a="ALL" onclick="sA('ALL',this)">ALL</button>
        <button class="fb" data-a="MAND" onclick="sA('MAND',this)">Mandatory Exit</button>
        <button class="fb" data-a="ROT" onclick="sA('ROT',this)">Rotation Exit</button>
        <button class="fb" data-a="BUY" onclick="sA('BUY',this)">New Entry</button>
        <button class="fb" data-a="HOLD" onclick="sA('HOLD',this)">Hold</button>
      </div>
      <div class="fsep"></div>
      <div class="fl">Scenario</div>
      <div class="fbtns" id="rbtns">
        <button class="fb fa-all" data-r="ALL" onclick="sR('ALL',this)">ALL</button>
        <button class="fb" data-r="S1" onclick="sR('S1',this)">S1</button>
        <button class="fb" data-r="S2" onclick="sR('S2',this)">S2</button>
        <button class="fb" data-r="S3" onclick="sR('S3',this)">S3</button>
        <button class="fb" data-r="ANY" onclick="sR('ANY',this)">Any</button>
      </div>
      <div class="fsep"></div>
      <div class="fl">Exchange</div>
      <select id="fex" onchange="R()">
        <option value="ALL">All</option>
        <option>NYSE</option><option>NASDAQ</option><option>XETRA</option>
        <option>LSE</option><option>EURONEXT</option><option>CRYPTO</option>
      </select>
      <div class="rct" id="rct"><span>\u2014</span> rows</div>
    </div>
    <div class="leg">
      <span class="leg-t">Action:</span>
      <div class="leg-i"><div class="leg-d" style="background:var(--cm)"></div>Mandatory Exit (P1 stop \u00b7 P2 death cross \u00b7 P3 ADX weak)</div>
      <div class="leg-i"><div class="leg-d" style="background:var(--cr)"></div>Rotation Exit (P4 rank drop)</div>
      <div class="leg-i"><div class="leg-d" style="background:var(--cb2)"></div>New Entry (limit close+0.5%)</div>
      <div class="leg-i"><div class="leg-d" style="background:var(--ch)"></div>Hold</div>
      <span style="margin-left:8px" class="leg-t">Scenario:</span>
      <div class="leg-i"><span class="sb sb-s1">S1</span>Pure Momentum</div>
      <div class="leg-i"><span class="sb sb-s2">S2</span>Force Diversity</div>
      <div class="leg-i"><span class="sb sb-s3">S3</span>Balanced</div>
    </div>
    <div class="tw">
      <table>
        <thead><tr>
          <th class="tl" onclick="srt('rank')"># <span class="sa" id="a-rank">\u21d5</span></th>
          <th class="tl" style="min-width:150px">Symbol</th>
          <th class="tl">Action</th>
          <th class="tl">Reason / Order</th>
          <th class="tl">Scenarios</th>
          <th class="tl">Exch</th>
          <th onclick="srt('momentum_score')">Score <span class="sa" id="a-momentum_score">\u21d5</span></th>
          <th onclick="srt('adx')">ADX <span class="sa" id="a-adx">\u21d5</span></th>
          <th onclick="srt('atrP')">ATR% <span class="sa" id="a-atrP">\u21d5</span></th>
          <th>Entry / Stop</th>
          <th onclick="srt('position_pct')">Pos% <span class="sa" id="a-position_pct">\u21d5</span></th>
          <th onclick="srt('pnl')">P&amp;L <span class="sa" id="a-pnl">\u21d5</span></th>
          <th onclick="srt('roc_60d')">60d Ret <span class="sa" id="a-roc_60d">\u21d5</span></th>
        </tr></thead>
        <tbody id="tb"></tbody>
      </table>
    </div>
  </div>
</div>
<div class="ftr">
  <div>TrendFollowing OS v3.9 \u00b7 Script 12 HTML Output \u00b7 Architecture v3.2</div>
  <div id="ftr-formula">\u2014</div>
  <div>\u26a0 HUMAN APPROVAL REQUIRED \u00b7 Exits first \u2192 Entries \u00b7 Script 12 also generates PDF</div>
</div>
<script>
const STRATEGIES=/*INJECT_STRATEGIES*/;
const SK=['scenario_1_pure_momentum','scenario_2_force_diversity','scenario_3_balanced'];
const SL=['S1','S2','S3'];
const STCLS=['st1','st2','st3','st4','st5'];
const BLIST=[{key:'portfolio_drawdown',lbl:'DD < \u221215%'},{key:'vix_spike',lbl:'VIX \u2265 40'},{key:'correlation_breakdown',lbl:'Corr > 0.85'},{key:'concentration_creep',lbl:'Top-3 > 30%'},{key:'data_staleness',lbl:'Data stale'}];
let aS=0,showCmp=false,fA='ALL',fR='ALL',sortK='ao',sortD=1;
function init(){
  renderBar();switchStrat(0);
}
function renderBar(){
  const bar=document.getElementById('strat-bar');
  const btn=document.getElementById('cmp-btn');
  [...bar.querySelectorAll('.stab')].forEach(e=>e.remove());
  STRATEGIES.forEach((s,i)=>{
    const t=document.createElement('div');t.className='stab';t.id='stab-'+i;
    t.innerHTML=`<div><div style="display:flex;align-items:center;gap:6px"><span class="stab-name">${s.name}</span><span class="sbadge ${s.deployed?'sb-live':'sb-paper'}">${s.deployed?'LIVE':'PAPER'}</span></div><div style="display:flex;align-items:center;gap:5px;margin-top:2px"><span class="stab-label">${s.label}</span><span class="stab-formula">${s.formula}</span></div></div>`;
    t.onclick=()=>{showCmp=false;updCmpBtn();document.getElementById('cmp-view').classList.add('hidden');document.getElementById('single-view').classList.remove('hidden');switchStrat(i);};
    bar.insertBefore(t,btn);
  });
}
function switchStrat(i){
  aS=i;const s=STRATEGIES[i];const cls=STCLS[i]||'st1';
  STRATEGIES.forEach((_,j)=>{const t=document.getElementById('stab-'+j);if(t){t.className='stab';if(j===i)t.classList.add('act-'+cls);}});
  const rec=s.rec;
  document.getElementById('hdr-m').textContent='Run: '+rec.rebalance_date+' \u00b7 Exec: '+rec.execution_date+' \u00b7 '+rec.architecture_version;
  document.getElementById('deploy-lbl').textContent=s.deployed?'LIVE':'PAPER TRADING';
  document.getElementById('deploy-lbl').style.color=s.deployed?'var(--green)':'var(--yellow)';
  document.getElementById('kv-eq').textContent='\u20ac'+(rec.account_equity||0).toLocaleString('de-DE');
  document.getElementById('kv-q').textContent=(rec.universe||{}).total_qualified||'\u2014';
  document.getElementById('kv-mp').textContent=rec.max_positions||'\u2014';
  const s1=rec[SK[0]]||{};
  document.getElementById('kv-me').textContent=((s1.exits||{}).mandatory||[]).length;
  document.getElementById('kv-e1').textContent=((rec[SK[0]]||{}).entries||{}).total||0;
  document.getElementById('kv-e2').textContent=((rec[SK[1]]||{}).entries||{}).total||0;
  document.getElementById('kv-e3').textContent=((rec[SK[2]]||{}).entries||{}).total||0;
  document.getElementById('kv-ed').textContent=rec.execution_date||'\u2014';
  document.getElementById('ftag-wrap').innerHTML=`<div class="ftag ft-${cls}"><div class="ft-dot" style="background:var(--${cls}c)"></div><strong>${s.name}</strong>&nbsp;\u00b7&nbsp;${s.formula}&nbsp;\u00b7&nbsp;${s.formula_desc}${s.roc_weights?' \u00b7 weights: ['+s.roc_weights.join(', ')+']':''}</div>`;
  document.getElementById('ftr-formula').textContent=s.formula_desc;
  renderCB((rec.circuit_breakers||{}));
  renderScenarios(s);
  R();
  if(showCmp)renderCmp();
}
function renderCB(cb){
  const dot=document.getElementById('cb-dot'),t=document.getElementById('cb-t');
  const kv=document.getElementById('kv-cb'),ks=document.getElementById('ks-cb');
  if(cb.halt_all){dot.className='cb-dot cb-halt';t.textContent='HALT ALL';t.style.color='var(--red)';kv.textContent='HALT';kv.className='kv c-red';ks.textContent='all suspended';}
  else if(cb.halt_entries){dot.className='cb-dot cb-warn';t.textContent='ENTRIES HALTED';t.style.color='var(--yellow)';kv.textContent='WARN';kv.className='kv c-yellow';ks.textContent='exits only';}
  else{dot.className='cb-dot cb-clear';t.textContent='All clear';t.style.color='var(--green)';kv.textContent='CLEAR';kv.className='kv c-green';ks.textContent=(cb.breakers||[]).length+' triggered';}
  const trg=new Set((cb.breakers||[]).map(b=>b.breaker));
  document.getElementById('cb-items').innerHTML=BLIST.map(b=>{
    if(trg.has(b.key)){const bd=(cb.breakers||[]).find(x=>x.breaker===b.key)||{};const c=bd.severity==='CRITICAL'?'cbi-crit':'cbi-warn';return`<div class="cbi ${c}"><div class="cbi-d ${c==='cbi-crit'?'cb-halt':'cb-warn'}"></div>${b.lbl}</div>`;}
    return`<div class="cbi cbi-ok"><div class="cbi-d cb-clear"></div>${b.lbl}</div>`;
  }).join('');
}
function renderScenarios(s){
  const cls=['sc-s1','sc-s2','sc-s3'];
  document.getElementById('srow').innerHTML=SK.map((k,i)=>{
    const sc=s.rec[k]||{};const cs=sc.capital_summary||{};
    const m=((sc.exits||{}).mandatory||[]).length,ro=((sc.exits||{}).rotation||[]).length;
    const en=((sc.entries||{}).total||0),ho=((sc.holds||{}).total||0),wn=((sc.warnings)||[]).length;
    return`<div class="scard ${cls[i]}" onclick="sR('${SL[i]}',null)"><div class="sc-name">${SL[i]} \u00b7 ${sc.name||SL[i]}</div><div class="sc-phil">${sc.philosophy||''}</div><div class="sc-stats"><div class="sc-stat">Deployed <span>${(cs.total_deployed_after_pct||0).toFixed(1)}%</span></div><div class="sc-stat">Cash <span>${(cs.cash_remaining_approx_pct||0).toFixed(1)}%</span></div><div class="sc-stat">Pool <span>${sc.candidate_pool||'?'}</span></div></div><div class="sc-pills">${m?`<span class="spill sp-mand">${m} mand exit</span>`:''}${ro?`<span class="spill sp-rot">${ro} rot exit</span>`:''}${en?`<span class="spill sp-buy">${en} entry</span>`:''}${ho?`<span class="spill sp-hold">${ho} hold</span>`:''}${wn?`<span class="spill sp-warn">\u26a0 ${wn}</span>`:''}</div></div>`;
  }).join('');
}
function toggleCmp(){showCmp=!showCmp;updCmpBtn();document.getElementById('cmp-view').classList.toggle('hidden',!showCmp);document.getElementById('single-view').classList.toggle('hidden',showCmp);if(showCmp)renderCmp();}
function updCmpBtn(){const b=document.getElementById('cmp-btn');b.classList.toggle('active',showCmp);b.textContent=showCmp?'\u2715 Close Compare':'\u21c4 Compare Strategies';}
function renderCmp(){
  const sl=fR==='ALL'||fR==='ANY'?'S1':fR;
  const sk=SK[SL.indexOf(sl)]||SK[0];
  const maps=STRATEGIES.map(s=>{
    const sc=s.rec[sk]||{};const m={};
    ((sc.exits||{}).mandatory||[]).forEach(e=>{m[e.symbol]='EXIT';});
    ((sc.exits||{}).rotation||[]).forEach(e=>{m[e.symbol]='ROT';});
    ((sc.entries||{}).new||[]).forEach(e=>{m[e.symbol]='BUY';});
    ((sc.holds||{}).positions||[]).forEach(h=>{m[h.symbol]='HOLD';});
    return m;
  });
  const allSyms=[...new Set(STRATEGIES.flatMap((_,i)=>Object.keys(maps[i])))].sort();
  const inAll=sym=>STRATEGIES.every((_,i)=>maps[i][sym]);
  const ncols=STRATEGIES.length;
  document.getElementById('cmp-grid').style.gridTemplateColumns=`repeat(${Math.min(ncols,4)},1fr)`;
  document.getElementById('cmp-grid').innerHTML=STRATEGIES.map((s,si)=>{
    const cls=STCLS[si]||'st1';
    const rows=allSyms.filter(sym=>maps[si][sym]).map(sym=>{
      const act=maps[si][sym];const both=inAll(sym);
      const ac={EXIT:'act-exit',ROT:'act-rot',BUY:'act-buy',HOLD:'act-hold'}[act]||'';
      const al={EXIT:'EXIT',ROT:'ROT EXIT',BUY:'BUY',HOLD:'HOLD'}[act]||act;
      return`<div class="cmp-row ${both?'both':'unique'}"><span class="cmp-sym">${sym}${both?' <span class="cmp-both">ALL</span>':''}</span><span class="cmp-act ${ac}">${al}</span></div>`;
    }).join('');
    return`<div class="cmp-col"><div class="cmp-col-hdr ch-${cls}">${s.name} \u00b7 ${s.label}<span style="font-size:9px;opacity:.7;font-weight:400;margin-left:4px">${sl} scenario</span></div><div class="cmp-sym-list">${rows||'<div class="empty">No actions</div>'}</div></div>`;
  }).join('');
}
function sA(a){fA=a;document.querySelectorAll('#abtns .fb').forEach(b=>{b.className='fb';if(b.dataset.a===a){const m={ALL:'fa-all',MAND:'fa-mand',ROT:'fa-rot',BUY:'fa-buy',HOLD:'fa-hold'};b.classList.add(m[a]||'fa-all');}});R();}
function sR(r){fR=r;document.querySelectorAll('#rbtns .fb').forEach(b=>{b.className='fb';if(b.dataset.r===r){const m={ALL:'fa-all',S1:'fr-s1',S2:'fr-s2',S3:'fr-s3',ANY:'fr-any'};b.classList.add(m[r]||'fa-all');}});R();if(showCmp)renderCmp();}
function srt(k){if(sortK===k)sortD*=-1;else{sortK=k;sortD=1;}document.querySelectorAll('th .sa').forEach(a=>a.textContent='\u21d5');const el=document.getElementById('a-'+k);if(el)el.textContent=sortD===1?'\u2191':'\u2193';R();}
function buildRows(s){
  const rows={};const rcMap={};(s.rec.ranked_candidates||[]).forEach(r=>{rcMap[r.symbol]=r;});
  SK.forEach((sk,si)=>{
    const sc=s.rec[sk]||{};const lbl=SL[si];
    ((sc.exits||{}).mandatory||[]).forEach(e=>{const k='M_'+e.symbol;if(!rows[k])rows[k]={...e,_act:'MAND',_sc:new Set(),ao:1,momentum_score:null,adx:null,atrP:null,roc_60d:null,position_pct:null,pnl:e.unrealized_pnl};rows[k]._sc.add(lbl);});
    ((sc.exits||{}).rotation||[]).forEach(e=>{const k='R_'+e.symbol+'_'+lbl;const rc=rcMap[e.symbol]||{};rows[k]={...e,_act:'ROT',_sc:new Set([lbl]),ao:2,momentum_score:rc.momentum_score||null,adx:rc.adx||null,atrP:rc.atr_pct||null,roc_60d:rc.roc_60d||null,position_pct:null,pnl:e.unrealized_pnl};});
    ((sc.entries||{}).new||[]).forEach(e=>{const k='B_'+e.symbol+'_'+lbl;rows[k]={...e,_act:'BUY',_sc:new Set([lbl]),ao:3,momentum_score:e.momentum_score,adx:e.adx,atrP:e.atr_pct||e.atrP,roc_60d:e.roc_60d,pnl:null};});
    ((sc.holds||{}).positions||[]).forEach(h=>{const k='H_'+h.symbol;const rc=rcMap[h.symbol]||{};if(!rows[k])rows[k]={...h,_act:'HOLD',_sc:new Set(),ao:4,momentum_score:rc.momentum_score||null,adx:rc.adx||null,atrP:rc.atr_pct||null,roc_60d:rc.roc_60d||null,position_pct:null,pnl:h.unrealized_pnl};rows[k]._sc.add(lbl);});
  });
  Object.values(rows).forEach(r=>{const rc=rcMap[r.symbol];if(!rc)return;if(r.momentum_score===null)r.momentum_score=rc.momentum_score;if(r.adx===null)r.adx=rc.adx;if(r.atrP===null)r.atrP=rc.atr_pct;if(r.roc_60d===null)r.roc_60d=rc.roc_60d;if(!r.rank)r.rank=rc.rank;if(!r.exchange)r.exchange=rc.exchange;if(!r.asset_class)r.asset_class=rc.asset_class;if(!r.name)r.name=rc.name;});
  return Object.values(rows);
}
function R(){
  const s=STRATEGIES[aS];const cls=STCLS[aS]||'st1';
  const q=(document.getElementById('srch').value||'').toLowerCase();
  const exch=document.getElementById('fex').value;
  let rows=buildRows(s).filter(r=>{
    if(q&&!r.symbol.toLowerCase().includes(q)&&!(r.name||'').toLowerCase().includes(q))return false;
    if(exch!=='ALL'&&r.exchange!==exch)return false;
    if(fA!=='ALL'){if(fA==='MAND'&&r._act!=='MAND')return false;if(fA==='ROT'&&r._act!=='ROT')return false;if(fA==='BUY'&&r._act!=='BUY')return false;if(fA==='HOLD'&&r._act!=='HOLD')return false;}
    if(fR!=='ALL'){if(fR==='ANY'){if(r._sc.size===0)return false;}else if(!r._sc.has(fR))return false;}
    return true;
  });
  rows.sort((a,b)=>{const av=a[sortK],bv=b[sortK];if(av===null&&bv===null)return 0;if(av===null)return 1;if(bv===null)return-1;if(typeof av==='string')return sortD*av.localeCompare(bv);return sortD*(av-bv);});
  document.getElementById('rct').innerHTML=`<span>${rows.length}</span> rows`;
  const tb=document.getElementById('tb');
  if(!rows.length){tb.innerHTML=`<tr><td colspan="13" class="empty"><span class="empty-i">\u2205</span>No rows match current filters</td></tr>`;return;}
  tb.innerHTML=rows.map(r=>row(r,cls)).join('');
}
function row(r,cls){
  const RC={MAND:'row-mand',ROT:'row-rot',BUY:'row-buy',HOLD:'row-hold'};
  const CHIPS={MAND:'<span class="chip ch-mand">MANDATORY EXIT</span>',ROT:'<span class="chip ch-rot">ROTATION EXIT</span>',BUY:'<span class="chip ch-buy">NEW ENTRY</span>',HOLD:'<span class="chip ch-hold">HOLD</span>'};
  const rc=(STRATEGIES[aS].rec.ranked_candidates||[]).find(x=>x.symbol===r.symbol);
  const rnk=r.momentum_rank||(rc&&rc.rank)||'\u2014';
  const prioM={1:'p1',2:'p2',3:'p3',4:'p4'};
  const prioEl=r.priority?`<div class="prio ${prioM[r.priority]||'p4'}">P${r.priority}</div>`:'';
  const ac=r.asset_class?`<span class="ap">${r.asset_class.slice(0,3).toUpperCase()}</span>`:'';
  const symEl=`<div><div class="sym">${r.symbol}${ac}</div><div class="snm">${r.name||''}</div></div>`;
  const RM={stop_loss_hit:['rt-stop','STOP HIT'],trend_reversal:['rt-rev','DEATH CROSS'],trend_weakness:['rt-weak','ADX WEAK'],dropped_from_top_n:['rt-drop','RANK DROP']};
  const[rc2,rl]=(r.reason&&RM[r.reason])||['',''];
  const rEl=r.reason?`<span class="rt ${rc2}">${rl}</span>`:'';
  const oShort=(r.order_type||'').replace('limit_order_close_plus_0.5pct','limit close+0.5%').replace('market_order_at_open','market @ open').replace('market_order_at_close','market @ close');
  const oEl=r.order_type?`<div class="ot">${oShort}</div>`:'';
  const badges=[...(r._sc||[])].sort().map(s=>`<span class="sb sb-${s.toLowerCase()}">${s}</span>`).join('');
  const ms=r.momentum_score;let scoreEl='\u2014';
  if(ms!=null){const p=Math.min(100,Math.max(0,(Math.abs(ms)/35)*100)).toFixed(0);const c=ms>=0?'pos':'neg';const sg=ms>=0?'+':'';const bar=ms>=0?`<div class="sb-bg"><div class="sb-f sf-${cls}" style="width:${p}%"></div></div>`:'';scoreEl=`<div class="sbw">${bar}<span class="${c}">${sg}${ms.toFixed(1)}%</span></div>`;}
  let adxEl='\u2014';if(r.adx!=null){const c=r.adx>=35?'adxs':r.adx>=25?'adxm':'adxw2';adxEl=`<div class="adxw"><div class="adxp ${c}"></div><span>${r.adx.toFixed(1)}</span></div>`;}
  const atrEl=r.atrP!=null?r.atrP.toFixed(1)+'%':'\u2014';
  let estop='\u2014';
  if(r._act==='BUY'){const st=r.stop_type||'initial';estop=`<div class="stw"><span>Lmt: ${fmt(r.limit_price)}</span><span style="font-size:9px;color:var(--txt2)">Ref: ${fmt(r.entry_price)}</span><div style="display:flex;align-items:center;gap:3px;margin-top:1px"><span style="font-size:9px;color:var(--txt2)">Stop: ${fmt(r.initial_stop)}</span><span class="stb ${st==='trailing'?'st-trail':'st-init'}">${st}</span></div></div>`;}
  else if(r.entry_price||r.current_stop){const stop=r.current_stop||r.initial_stop;const st=r.stop_type||'initial';estop=`<div class="stw">${r.entry_price?`<span style="font-size:9px;color:var(--txt2)">Entry: ${fmt(r.entry_price)}</span>`:''}<div style="display:flex;align-items:center;gap:3px"><span>Stop: ${fmt(stop)}</span><span class="stb ${st==='trailing'?'st-trail':'st-init'}">${st}</span></div></div>`;}
  const posEl=r.position_pct!=null?r.position_pct.toFixed(2)+'%':'\u2014';
  let pnlEl='\u2014';if(r.pnl!=null){const c=r.pnl>=0?'pos':'neg';const sg=r.pnl>=0?'+':'';const pp=r.unrealized_pnl_pct;pnlEl=`<div style="display:flex;flex-direction:column;align-items:flex-end"><span class="${c}">${sg}\u20ac${Math.abs(r.pnl).toLocaleString('de-DE',{maximumFractionDigits:0})}</span>${pp!=null?`<span style="font-size:9px;color:var(--txt2)">${sg}${pp.toFixed(1)}%</span>`:''}</div>`;}
  let r60='\u2014';if(r.roc_60d!=null){const c=r.roc_60d>=0?'pos':'neg';r60=`<span class="${c}">${r.roc_60d>=0?'+':''}${r.roc_60d.toFixed(1)}%</span>`;}
  return`<tr class="${RC[r._act]||''}"><td class="tl" style="color:var(--txt3);font-size:10px;min-width:26px">${rnk}${prioEl?'<br>'+prioEl:''}</td><td class="tl">${symEl}</td><td class="tl">${CHIPS[r._act]}</td><td class="tl"><div style="display:flex;flex-direction:column;gap:2px">${rEl}${oEl}</div></td><td class="tl"><div class="sba">${badges||'<span style="color:var(--txt3);font-size:10px">\u2014</span>'}</div></td><td class="tl" style="font-size:10px;color:var(--txt2);font-family:var(--mono)">${r.exchange||'\u2014'}</td><td>${scoreEl}</td><td>${adxEl}</td><td>${atrEl}</td><td>${estop}</td><td>${posEl}</td><td>${pnlEl}</td><td>${r60}</td></tr>`;
}
function fmt(v){if(v==null)return'\u2014';if(v>=10000)return v.toLocaleString('de-DE',{maximumFractionDigits:0});if(v>=1000)return v.toLocaleString('de-DE',{minimumFractionDigits:0,maximumFractionDigits:0});return v.toFixed(2);}
init();
</script>
</body>
</html>"""


def build_html_dashboard(
    strategy_results: List[Tuple[Dict, str, str, bool, str]],
    output_path: Path,
) -> None:
    """
    Generate a combined interactive HTML dashboard from one or more
    strategy recommendation dicts (Script 11 output).

    Each call to this function produces a single self-contained HTML file
    that embeds ALL strategies as a JS constant array, enabling the
    strategy-switcher tab bar and side-by-side compare view.

    Args:
        strategy_results:
            List of (rec_dict, strategy_name, strategy_label, deployed, formula).
            Collected by main() from each successful _run_core() call.
        output_path:
            Destination path, e.g. reports/rebalancing/2026-03_dashboard.html

    Raises:
        IOError on write failure.
    """
    if not strategy_results:
        logger.warning("build_html_dashboard: no results supplied — skipping.")
        return

    # Build the JS STRATEGIES array literal
    strat_objects = []
    for rec, name, label, deployed, formula in strategy_results:
        roc_weights = None
        if formula in ("roc_weighted", "roc_weight"):
            roc_weights = rec.get("momentum", {}).get("roc_weights", [0.20, 0.30, 0.50])

        obj = {
            "name":         name,
            "label":        label,
            "deployed":     deployed,
            "formula":      formula,
            "formula_desc": _formula_desc(formula, rec),
            "roc_weights":  roc_weights,
            "rec":          _slim_rec(rec),
        }
        strat_objects.append(json.dumps(obj, ensure_ascii=False, default=str))

    strategies_js = "[\n" + ",\n".join(strat_objects) + "\n]"

    # Inject into template
    html = _dashboard_template_html()
    html = html.replace(_JS_SENTINEL, strategies_js)
    html = html.replace(
        _TS_SENTINEL,
        f"<!-- Generated by Script 12 at "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
        f"| {len(strategy_results)} strateg"
        f"{'y' if len(strategy_results) == 1 else 'ies'} -->",
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    logger.info(
        f"HTML dashboard written ({output_path.stat().st_size / 1024:.1f} KB) "
        f"-> {output_path}"
    )


# ============================================================================
# CLI
# ============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Script 12: Recommendation Report Generator\n"
            "Converts Script 11 JSON output into a formatted PDF report."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-detect latest recommendations
  python scripts/12_generate_recommendation_report.py

  # Specific month
  python scripts/12_generate_recommendation_report.py --month 2026-01

  # Specific JSON file
  python scripts/12_generate_recommendation_report.py \\
      --input reports/rebalancing/2026-01_recommendations.json

  # Custom output path
  python scripts/12_generate_recommendation_report.py \\
      --month 2026-01 --output /tmp/2026-01_report.pdf

  # Dry-run: validate JSON only (no PDF written)
  python scripts/12_generate_recommendation_report.py --month 2026-01 --dry-run
        """,
    )
    parser.add_argument(
        "--month",
        type=str,
        default=None,
        metavar="YYYY-MM",
        help="Rebalancing month (e.g. 2026-01). Locates {month}_recommendations.json automatically.",
    )
    parser.add_argument(
        "--input",
        type=str,
        default=None,
        metavar="PATH",
        help="Explicit path to a recommendations JSON file (overrides --month).",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "Output path for the generated PDF. "
            "Default: same directory and name as the input JSON, with .pdf extension."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Validate the input JSON and report its contents without writing a PDF.",
    )
    add_strategy_argument(parser)
    return parser.parse_args()


# ============================================================================
# MAIN
# ============================================================================

def _run_core(args, strategy_name: str = "", strategy_obj=None):
    """
    Run PDF generation for one strategy.

    Returns:
        (rec, strategy_name, label, deployed, formula)  -- on success
        None                                             -- on failure or dry-run
    """
    try:
        json_path = resolve_input_file(args.month, args.input)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        return None

    month_label = json_path.stem.replace("_recommendations", "")

    global logger
    _log_label = f"{month_label}_{strategy_name}" if strategy_name else month_label
    logger = setup_logging(_log_label)

    logger.info("=" * 65)
    logger.info("Script 12  |  Recommendation Report Generator  |  v3.2")
    logger.info("=" * 65)
    logger.info(f"Input  : {json_path}")

    try:
        rec = load_recommendations(json_path)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error(f"Failed to load recommendations JSON: {exc}")
        return None

    status = rec.get("status", "")
    s1     = rec.get("scenario_1_pure_momentum") or {}
    n_mand = len((s1.get("exits") or {}).get("mandatory", []))
    n_rot  = len((s1.get("exits") or {}).get("rotation",  []))
    n_ent  = (s1.get("entries") or {}).get("total", 0)
    n_hold = (s1.get("holds")   or {}).get("total", 0)

    logger.info(f"Month  : {rec.get('month', chr(8212))}")
    logger.info(f"Status : {status}")
    logger.info(f"Equity : EUR {rec.get('account_equity', 0):,.2f}")
    logger.info(
        f"Actions (S1): {n_mand} mandatory exits, {n_rot} rotation exits, "
        f"{n_ent} new entries, {n_hold} holds"
    )

    if args.dry_run:
        logger.info("DRY RUN -- JSON validated. No PDF or HTML written.")
        print(f"\nDRY RUN complete. Input is valid: {json_path}")
        print(f"  Status : {status}")
        return None

    if args.output:
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = PROJECT_ROOT / output_path
    else:
        sfx = f"_{strategy_name}" if strategy_name else ""
        output_path = json_path.parent / f"{json_path.stem}{sfx}.pdf"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        build_pdf(rec, output_path, strategy_name=strategy_name)
    except Exception as exc:
        logger.exception(f"PDF generation failed: {exc}")
        return None

    logger.info(f"PDF saved -> {output_path}")
    print(f"\n{'=' * 65}")
    print(f"  Script 12 [{strategy_name or 'default'}]")
    print(f"  PDF  -> {output_path}")
    print(f"  JSON -> {json_path}")
    print(f"{'=' * 65}\n")

    label    = strategy_name
    deployed = False
    formula  = "sma_distance"
    if strategy_obj is not None:
        label    = getattr(strategy_obj, "label",    strategy_name)
        deployed = getattr(strategy_obj, "deployed", False)
        formula  = _load_formula_from_strategy(strategy_obj)

    return (rec, strategy_name, label, deployed, formula)


def main() -> int:
    args = parse_args()
    logger.info("=" * 70)
    logger.info("Script 12 -- Architecture v3.9 (Mar 2026)")
    logger.info("=" * 70)

    try:
        strategies = resolve_strategies(
            getattr(args, "strategy", None),
            project_root=PROJECT_ROOT,
        )
    except (FileNotFoundError, ValueError) as exc:
        logger.error(f"Strategy resolution failed: {exc}")
        return 1

    logger.info(f"Strategies : {[s.name for s in strategies]}")
    from datetime import datetime as _dt
    _start = _dt.now()

    failed: List[str]  = []
    strategy_results: List[Tuple] = []

    for strategy in strategies:
        global REPORTS_DIR
        strat_reports = strategy.reports_dir(PROJECT_ROOT, "rebalancing")
        strat_reports.mkdir(parents=True, exist_ok=True)
        logger.info(
            f"\n[{strategy.name}] -- {strategy.label} "
            f"({'LIVE' if strategy.deployed else 'PAPER'}) --"
        )
        logger.info(f"[{strategy.name}] Reports : {strat_reports}")
        _orig_rep   = REPORTS_DIR
        REPORTS_DIR = strat_reports
        try:
            result = _run_core(args, strategy.name, strategy_obj=strategy)
        finally:
            REPORTS_DIR = _orig_rep
        if result is None:
            failed.append(strategy.name)
        else:
            strategy_results.append(result)

    if strategy_results and not args.dry_run:
        first_rec     = strategy_results[0][0]
        month_lbl     = first_rec.get("month", "unknown")
        first_strat   = strategies[0]
        html_base_dir = first_strat.reports_dir(PROJECT_ROOT, "rebalancing").parent
        html_path     = html_base_dir / f"{month_lbl}_dashboard.html"
        n = len(strategy_results)
        logger.info(
            f"\nGenerating combined HTML dashboard "
            f"({n} strat{'egy' if n == 1 else 'egies'}: "
            f"{', '.join(r[1] for r in strategy_results)})..."
        )
        try:
            build_html_dashboard(strategy_results, html_path)
            print(f"  HTML -> {html_path}")
        except Exception as exc:
            logger.warning(f"HTML dashboard generation failed (non-fatal): {exc}")

    elapsed = _dt.now() - _start
    logger.info(
        f"Duration: {elapsed} | Strategies: {len(strategies)} | Failed: {failed or 'none'}"
    )
    logger.info("Next step: review PDF + HTML dashboard, then execute on execution_date.")
    return 1 if failed else 0
if __name__ == "__main__":
    main()
